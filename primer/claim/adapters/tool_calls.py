"""ClaimAdapter for ClaimKind.TOOL_CALL (Phase 3 stage 7a).

Entirely additive and, on its own, inert: nothing creates ToolCallTask
rows or TOOL_CALL leases yet (that lands in the dispatch-seam split, a
later commit in this arc). This commit lands the adapter + entity so the
claim-engine wiring, eligibility filter, and terminal-state bookkeeping
are independently reviewable and tested ahead of the riskier executor
change.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.int.storage import Storage
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState


class ToolCallClaimAdapter(ClaimAdapter):
    kind = ClaimKind.TOOL_CALL
    entity_table = "toolcalltask"

    def __init__(self, *, task_storage: Storage | None) -> None:
        self._storage = task_storage

    def eligibility_sql(self) -> str:
        # ``state`` lives inside the entity's JSONB ``data`` column, not a
        # top-level column (matches every other adapter's convention - a
        # bare ``e.state`` reference raises UndefinedColumnError on
        # Postgres and breaks the WHOLE claim loop, not just this kind).
        #
        # Excludes GATED (approval/yield unsatisfied - the whole point of
        # task-granular gating: this ONE row is ineligible, its batch
        # siblings are not) and terminal states (DONE/FAILED, nothing left
        # to claim). RUNNING is also excluded under normal operation - a
        # live lease already makes it unclaimable via the shared
        # ``claimed_by IS NULL OR expires_at < now()`` guard in sql.py, so
        # admitting it here only matters for a crashed worker's expired
        # lease, which is exactly the reclaim-for-retry case and SHOULD be
        # eligible again.
        return "e.data->>'state' IN ('queued', 'running')"

    def entity_indexes(self, qualified_table: str) -> list[str]:
        # Backs the eligibility filter above (runs every claim cycle) and
        # the dispatch seam's own "all tasks for this turn done?" query
        # (session_id + turn_no, used to decide when to re-arm the
        # session - see the design doc's on_release rule). Both partial /
        # IF NOT EXISTS, matching the session adapter's own park-index
        # convention: cheap on the common (terminal) case, safe to race.
        return [
            f"CREATE INDEX IF NOT EXISTS idx_toolcalltask_state "
            f"ON {qualified_table} ((data->>'state')) "
            f"WHERE data->>'state' IN ('queued', 'running')",
            f"CREATE INDEX IF NOT EXISTS idx_toolcalltask_turn "
            f"ON {qualified_table} ((data->>'session_id'), (data->>'turn_no'))",
        ]

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        if self._storage is None:
            raise RuntimeError(
                "task_storage is None - cannot run on_release without a storage backend"
            )
        task = await self._storage.get(entity_id, conn=conn)
        if task is None:
            return

        now = datetime.now(timezone.utc)

        # Gate branch: the tool call hit an approval/yield gate mid-
        # execution (a yielding tool, or an approval-required call) -
        # mirrors the session adapter's own park branch. The engine drops
        # the lease (drop_lease=True on this outcome); the gate/wake event
        # re-arms it later via engine.mark_resumable on THIS task's own
        # entity_id, not the session's - the reason one gated call no
        # longer blocks its siblings.
        if outcome.park is not None:
            p = outcome.park
            gated = task.model_copy(update={
                "state": ToolCallTaskState.GATED,
                "gate_event_key": p.parked_event_key,
                "gate_until": p.parked_until,
            })
            await self._storage.update(gated, conn=conn)
            return

        # Terminal branch: the caller has already decided this task is
        # done (whether the underlying tool call itself succeeded or
        # failed is caller's call, encoded in outcome.success - the
        # dispatch seam writes the durable TOOL_RESULT record, success OR
        # a poisoned-task failure, BEFORE calling release; this row only
        # needs a cheap, queryable summary of which happened).
        if outcome.drop_lease:
            updated = task.model_copy(update={
                "state": (
                    ToolCallTaskState.DONE if outcome.success
                    else ToolCallTaskState.FAILED
                ),
                "finished_at": now,
                "last_error": None if outcome.success else outcome.last_error,
            })
            await self._storage.update(updated, conn=conn)
            return

        # Retryable branch: not gated, not terminal - a transient failure
        # (reclaim, worker crash) or an explicit requeue. Reset to QUEUED
        # so the next claim (this worker or another) picks it up again;
        # the engine's own lease.attempt_count is the authoritative retry
        # counter (see WorkerConfig.max_attempts) - this row does not
        # duplicate that bookkeeping, it only needs to stop reading
        # RUNNING once nobody is actually running it.
        retried = task.model_copy(update={
            "state": ToolCallTaskState.QUEUED,
            "started_at": None,
        })
        await self._storage.update(retried, conn=conn)


__all__ = ["ToolCallClaimAdapter"]
