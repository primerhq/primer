"""ClaimAdapter for ClaimKind.TOOL_CALL (Phase 3 stage 7a).

Dormant behind ``WorkerConfig.tool_calls_as_claims_enabled`` (default
off). With the flag on, the dispatch seam's park handlers
(``primer.session.dispatch`` and
``primer.session.persistence.materialize_pending_tool_wait_rows``) create
the ToolCallTask rows and upsert the TOOL_CALL leases this adapter
governs, and ``on_release`` below is the gate / terminal / retry
bookkeeping plus the last-sibling wake.

Every ``on_release`` branch is ONE field-scoped ``Storage.patch_if``, fenced on the row still
being QUEUED or RUNNING and still carrying the releaser's per-claim ``claim_token``. A release
whose claim was cancelled, deleted or taken over underneath it writes nothing and wakes nothing,
and a release with no token never matches.

NOT BUILT: nothing claims and runs a task. ``WorkerPool`` registers no
TOOL_CALL handler, so on main only tests ever release a TOOL_CALL lease
and the transitions below are exercised by hand. See "Tool-call claims:
built and not built" in docs/dev/architecture/claim-machine.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic_core import to_jsonable_python

from primer.int.claim import ClaimAdapter, ClaimKind, PostReleaseWake, ReleaseOutcome
from primer.int.storage import Storage
from primer.model.except_ import NotFoundError
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState

logger = logging.getLogger(__name__)

# The rows a release may move: not yet finished, not gated away. Everything else (gated, done,
# failed) is somebody else's state and a release leaves it alone.
_LIVE_STATES = [ToolCallTaskState.QUEUED.value, ToolCallTaskState.RUNNING.value]

# What a handler may put in ``ReleaseOutcome.entity_update``, per branch. ``state``, ``claim_token``, the gate
# fields and ``finished_at`` belong to the branches below (the adapter stamps ``finished_at`` itself, one clock);
# letting a caller write them would defeat the fence. A requeue clears ``result_state`` on purpose, so it may not
# supply one; a clean gate changes neither the result nor the unclean-execution count, so it takes nothing.
_ENTITY_UPDATE_KEYS = {
    "gated": frozenset(),
    "terminal": frozenset({"result_state", "attempts", "last_error"}),
    "retry": frozenset({"attempts", "last_error"}),
}


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

    def dead_lease_sql(self) -> str | None:
        # A lease of a finished task can never be claimed again (the eligibility filter above
        # excludes it), so it is garbage. A GATED task is NOT dead: its lease is re-armed when
        # the gate flips back to QUEUED, and the engine's own missing-entity rule covers a task
        # row that is gone.
        return "e.data->>'state' IN ('done', 'failed')"

    async def is_dead(self, entity_id: str) -> bool:
        if self._storage is None:
            return False
        task = await self._storage.get(entity_id)
        return task is None or task.state in (ToolCallTaskState.DONE, ToolCallTaskState.FAILED)

    async def on_release(
        self, conn, entity_id: str, *, outcome: ReleaseOutcome,
    ) -> "PostReleaseWake | None":
        if self._storage is None:
            raise RuntimeError(
                "task_storage is None - cannot run on_release without a storage backend"
            )
        # Validated before anything else, so a malformed update is loud even on a path that
        # would otherwise not write.
        branch = "gated" if outcome.park is not None else "terminal" if outcome.drop_lease else "retry"
        update = self._entity_update(outcome, branch)
        token = outcome.claim_token
        if token is None:
            # A None token never matches a fence. Reading it as "absent or null" in the predicate
            # would let a token-less release FAIL a fresh QUEUED row, so no row write is attempted.
            # A caller that only wants its lease back uses ``ReleaseOutcome(entity_noop=True)``,
            # which never reaches an adapter.
            logger.warning(
                "tool-call release of %s carries no claim token: the row is left untouched",
                entity_id,
            )
            return None
        fence = {"state": list(_LIVE_STATES), "claim_token": [token]}
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
            # gate_seq is a counter: a read, then a write that names the value read, so a
            # concurrent change rejects the write instead of being lost. A row stored before
            # the field existed has no ``gate_seq`` key and reads as 0; ``patch_if`` lets a guard
            # naming the default of a field that cannot hold null (``[0]`` here) also match the
            # absent key, so such a row can still be gated (tests pin it on the fake and on real
            # SQLite). Nothing else writes the counter while the row is RUNNING under our token.
            current = await self._read(entity_id, conn)
            if current is None:
                return None
            patch = {
                "state": ToolCallTaskState.GATED.value,
                "gate_event_key": p.parked_event_key,
                "gate_until": p.parked_until,
                # Ruling 3 (01a0518b): the gate's live payload blob - same
                # shape session/graph parks already stash on parked_state,
                # here on the task since a bare ToolCallTask has no other
                # durable home to point at. Cleared on every other branch
                # below so it never survives past the gate it belongs to.
                "gate_state": p.parked_state,
                "gate_seq": current.gate_seq + 1,
                # Nobody owns a gated row: the token dies with the claim, so the releaser
                # cannot write it again once the gate flips it back to QUEUED.
                "claim_token": None,
                **update,
            }
            await self._patch(
                entity_id, patch, {**fence, "gate_seq": [current.gate_seq]}, conn,
            )
            return None

        # Terminal branch: the caller has already decided this task is
        # done (whether the underlying tool call itself succeeded or
        # failed is caller's call, encoded in outcome.success; this row
        # only needs a cheap, queryable summary of which happened). The
        # tool's result travels in result_state, which the resume
        # coordinator turns into the TOOL_RESULT record. The intended
        # caller is the claim worker, which is not built: a poisoned-task
        # failure (retry cap) has no write side yet, only the resume
        # side's synthesised error part.
        if outcome.drop_lease:
            patch = {
                "state": (
                    ToolCallTaskState.DONE if outcome.success
                    else ToolCallTaskState.FAILED
                ).value,
                "finished_at": now,
                "last_error": None if outcome.success else outcome.last_error,
                "gate_state": None,
                **update,
            }
            updated = await self._patch(entity_id, patch, fence, conn)
            if updated is None:
                return None
            return await self._last_sibling_wake_signal(updated, conn=conn)

        # Retryable branch: not gated, not terminal - a transient failure
        # (reclaim, worker crash), a drain requeue or an explicit requeue. Reset to QUEUED
        # so the next claim (this worker or another) picks it up again;
        # the engine's own lease.attempt_count is the authoritative retry
        # counter for the LEASE (see WorkerConfig.max_attempts); the row's ``attempts`` counts
        # unclean executions and arrives, when it changes, through ``entity_update``.
        # result_state is also cleared here: a task reaching this branch
        # was RUNNING (a live lease existed and either expired or the
        # caller explicitly requeued it) - any result_state already
        # written by that attempt is from a run that never reached a
        # clean release and must not survive into the next attempt as
        # if it were authoritative.
        patch = {
            "state": ToolCallTaskState.QUEUED.value,
            "started_at": None,
            "gate_state": None,
            "result_state": None,
            "claim_token": None,
            **update,
        }
        await self._patch(entity_id, patch, fence, conn)
        return None

    @staticmethod
    def _entity_update(outcome: ReleaseOutcome, branch: str) -> dict[str, Any]:
        raw = outcome.entity_update
        if not raw:
            return {}
        allowed = _ENTITY_UPDATE_KEYS[branch]
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"a {branch} ToolCallClaimAdapter release takes entity_update keys {sorted(allowed)}, "
                f"got {sorted(unknown)}"
            )
        return dict(raw)

    async def _read(self, entity_id: str, conn) -> ToolCallTask | None:
        task = await self._storage.get(entity_id, conn=conn)
        if task is None:
            logger.warning("tool-call release of %s: the row is gone, nothing written", entity_id)
        return task

    async def _patch(
        self, entity_id: str, patch: dict[str, Any], where: dict[str, list[Any]], conn,
    ) -> ToolCallTask | None:
        """One fenced ``patch_if``; a rejected or missing row logs and returns ``None``."""
        try:
            updated = await self._storage.patch_if(
                entity_id, to_jsonable_python(patch), where=where, conn=conn,
            )
        except NotFoundError:
            logger.warning("tool-call release of %s: the row is gone, nothing written", entity_id)
            return None
        if updated is None:
            logger.warning(
                "tool-call release of %s rejected by its fence: cancelled, finished or "
                "taken over by another claim; nothing written, no wake",
                entity_id,
            )
        return updated

    async def _last_sibling_wake_signal(
        self, task: ToolCallTask, *, conn,
    ) -> "PostReleaseWake | None":
        """Ruling 2 (01a0518b): the LAST outstanding task of a tool_wait
        batch re-arms the owning session's claim lease.

        Returns a :class:`~primer.int.claim.PostReleaseWake` signal for
        the ENGINE to act on AFTER this release's own transaction commits
        (01a0518b review hazard fix) - this method itself never touches
        the session row or calls worker-layer code; see that class's own
        docstring for why the split exists.

        ``task.batch_task_ids`` is empty for a ToolCallTask that never
        went through the claim-based dispatch seam (defensive - none
        exist in production yet, but a bare adapter unit test might
        construct one directly) - a no-op, exactly as if this task were
        never part of a batch at all.

        Reads every sibling via individual ``storage.get(id, conn=conn)``
        calls, NOT ``Storage.find`` (which has no ``conn`` parameter at
        all and would therefore read from OUTSIDE this release's own
        Postgres transaction - it could miss the very state THIS call
        just wrote, one line above, wrongly concluding "not last" when it
        is). Not the last sibling yet: returns ``None``, nothing to do -
        this task's own state is already durably updated above.

        ACCEPTED LIMITATION (7a gate review, item B - pinned by a
        transaction-spy test, no live database needed to reproduce):
        under Postgres's default READ COMMITTED isolation, a conn-scoped
        read only sees OTHER transactions' COMMITTED writes. If the LAST
        TWO siblings of a batch release genuinely concurrently - both
        transactions still open when the OTHER's sibling-check read here
        runs - EACH one observes the OTHER as not-yet-terminal and BOTH
        return ``None``. Neither concludes "I am last": the wake is lost
        outright, not merely delayed or double-fired (the double-fire
        case, where ONE sibling's transaction has already committed
        before the other's check runs, IS handled correctly and is
        idempotent - see the sibling test proving that). Structurally
        NOT reachable for ``InMemoryClaimEngine``: its storage fakes have
        no internal suspension point, so ``asyncio.gather`` always runs
        one release to full completion before the other starts, meaning
        one of the two always observes the other as terminal.

        The real fix would move the "am I last" decision from INSIDE
        this pre-commit read to a POST-COMMIT re-check (the same
        post-commit timing :class:`PostReleaseWake` already uses for
        firing the wake itself, extended to re-verifying its own
        precondition) - an architectural change to WHEN the decision
        happens, not a local patch, so it is deliberately NOT made here.
        Accepted degradation: same class as the documented crash window
        in :class:`PostReleaseWake`'s own docstring - it degrades to the
        existing park-timeout backstop. Recovery-boot reconciliation
        (task 01a07c06, upgraded to a flag-on prerequisite by this
        finding) is the intended eventual fix for BOTH that crash window
        and this concurrent-miss window; when it lands, the pinned test
        below should flip from asserting the loss to asserting recovery.
        """
        if not task.batch_task_ids or self._storage is None:
            return None
        for sibling_id in task.batch_task_ids:
            sibling = (
                task if sibling_id == task.id
                else await self._storage.get(sibling_id, conn=conn)
            )
            if sibling is None or sibling.state not in (
                ToolCallTaskState.DONE, ToolCallTaskState.FAILED,
            ):
                return None
        from primer.session.yields import tool_wait_event_key

        return PostReleaseWake(
            session_id=task.session_id,
            event_key=tool_wait_event_key(
                task.session_id, task.turn_no, scoped_task_id=task.id,
            ),
            payload={"tool_wait_ready": True},
        )


__all__ = ["ToolCallClaimAdapter"]
