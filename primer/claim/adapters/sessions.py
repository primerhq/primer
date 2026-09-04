from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.int.storage import Storage

if TYPE_CHECKING:
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.int.event_bus import EventBus
    from primer.model.workspace_session import WorkspaceSession

logger = logging.getLogger(__name__)


class SessionClaimAdapter(ClaimAdapter):
    kind = ClaimKind.SESSION
    entity_table = "sessions"

    def __init__(
        self,
        *,
        session_storage: Storage | None,
        workspace_registry: "WorkspaceRegistry | None" = None,
        event_bus: "EventBus | None" = None,
    ) -> None:
        self._storage = session_storage
        # 01a068ea: a workspace's I/O handle is resolved per-session (each
        # session belongs to its own workspace), so this adapter -- a
        # process-wide singleton spanning every workspace -- needs the
        # REGISTRY, not one fixed WorkspaceIO. Resolved lazily in
        # _write_terminal_record, mirroring
        # WorkerPool._load_workspace_for_persist's own per-call resolution.
        self._workspace_registry = workspace_registry
        self._event_bus = event_bus

    def eligibility_sql(self) -> str:
        # parked_status lives inside the entity's JSONB ``data`` column, not as
        # a top-level column. Use the JSONB accessor (matching the chat/harness/
        # trigger adapters); ``e.parked_status`` raises UndefinedColumnError on
        # Postgres and breaks the claim loop so no session ever runs.
        #
        # Admit two states:
        #   * parked_status IS NULL  -> a normal, never-parked session.
        #   * parked_status='resumable' -> a parked session the resume event
        #     has flipped; its lease was re-armed by engine.mark_resumable.
        # A 'parked' row is excluded so a parked session is not claimable on
        # Postgres. The in-memory engine's claim_due ignores this filter and
        # gates only on lease presence, so its no-loop guarantee comes from the
        # park branch dropping the lease (primer/session/dispatch.py), not from
        # this SQL. This filter is the Postgres-lane resume gate.
        return (
            "e.data->>'parked_status' IS NULL "
            "OR e.data->>'parked_status' = 'resumable'"
        )

    def entity_indexes(self, qualified_table: str) -> list[str]:
        # parked_status / parked_event_key / parked_event_keys all live inside
        # the JSONB ``data`` column, so Postgres needs expression indexes to
        # avoid sequential scans on the hot park paths:
        #   * the partial btree on parked_status backs the claim-eligibility
        #     filter (runs every claim cycle) and the listener's status guard;
        #   * the partial btree on parked_event_key backs the listener's
        #     primary keyed lookup (runs for every bus event);
        #   * the GIN on parked_event_keys backs the multi-event membership
        #     fallback (``Op.CONTAINS`` -> the jsonb ``?`` operator).
        # All are partial / IF NOT EXISTS so they cost nothing on unparked
        # rows and are safe to create repeatedly and race with peers.
        return [
            f"CREATE INDEX IF NOT EXISTS idx_sessions_parked_status "
            f"ON {qualified_table} ((data->>'parked_status')) "
            f"WHERE data->>'parked_status' IS NOT NULL",
            f"CREATE INDEX IF NOT EXISTS idx_sessions_parked_event_key "
            f"ON {qualified_table} ((data->>'parked_event_key')) "
            f"WHERE data->>'parked_event_key' IS NOT NULL",
            f"CREATE INDEX IF NOT EXISTS idx_sessions_parked_event_keys "
            f"ON {qualified_table} USING gin ((data->'parked_event_keys'))",
        ]

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        if self._storage is None:
            raise RuntimeError(
                "session_storage is None - cannot run on_release without a storage backend"
            )
        sess = await self._storage.get(entity_id, conn=conn)
        if sess is None:
            return

        # Park branch: the turn hit a yielding tool. Write the park columns
        # and clear the worker stamp; do NOT bump turn_no (a park is not a
        # completed turn). The engine drops the lease (drop_lease=True), so
        # the parked row has no lease and is not re-claimed until the resume
        # event re-arms it via engine.mark_resumable.
        if outcome.park is not None:
            p = outcome.park
            parked = sess.model_copy(update={
                "parked_status": "parked",
                "parked_event_key": p.parked_event_key,
                "parked_event_keys": p.parked_event_keys,
                "parked_until": p.parked_until,
                "parked_at": p.parked_at,
                "parked_state": p.parked_state,
                "last_worker_id": None,
            })
            await self._storage.update(parked, conn=conn)
            return

        # Preserve-park branch: the operator paused a resumable session (the
        # pause_requested gate in pool.py / run_one_session_turn). Drop the
        # lease but leave the park columns (parked_status stays 'resumable',
        # parked_state intact) so a later /resume can replay the hook. The
        # pause completion still counts as a turn, so bump turn_no /
        # last_turn_at on success, mirroring the non-park branch.
        if outcome.preserve_park:
            updates = {"last_worker_id": None}
            if outcome.success:
                updates["turn_no"] = sess.turn_no + 1
                updates["last_turn_at"] = datetime.now(timezone.utc)
            preserved = sess.model_copy(update=updates)
            await self._storage.update(preserved, conn=conn)
            return

        # Non-park release: clear any park columns. Only bump turn_no /
        # stamp last_turn_at when a turn actually ran (outcome.success).
        # A failed release (reclaim, executor build failure, executor crash)
        # must leave the counters untouched so the next claim sees the same
        # turn.
        updates: dict[str, object | None] = {
            "parked_status": None,
            "parked_event_key": None,
            "parked_event_keys": None,
            "parked_until": None,
            "parked_at": None,
            "parked_state": None,
            "last_worker_id": None,
        }
        if outcome.success:
            updates["turn_no"] = sess.turn_no + 1
            updates["last_turn_at"] = datetime.now(timezone.utc)

        updated = sess.model_copy(update=updates)
        await self._storage.update(updated, conn=conn)

        # Write a terminal error record to messages.jsonl when the release
        # is a failure (reclaim, worker crash, or any other engine error).
        # This is the ONLY durable error record for a crash/reclaim failure
        # mode: dispatch.py's own except-block write can't run if the
        # worker process that would run it is the thing that died.
        if not outcome.success and self._workspace_registry is not None:
            await self._write_terminal_record(sess, outcome)

    async def _write_terminal_record(
        self, session: "WorkspaceSession", outcome: ReleaseOutcome,
    ) -> None:
        """Append a synthetic error-kind SessionMessageRecord to messages.jsonl."""
        from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
        from primer.session.persistence import WorkspaceMessageWriter

        workspace_io = await self._workspace_registry.get_workspace(
            session.workspace_id,
        )
        if workspace_io is None:
            return

        reason = outcome.last_error or "unknown"
        record = SessionMessageRecord(
            seq=1,  # WorkspaceMessageWriter overwrites this
            kind=SessionMessageKind.ERROR,
            payload={"reason": reason, "terminal": True},
            created_at=datetime.now(timezone.utc),
        )
        writer = WorkspaceMessageWriter(
            workspace_io=workspace_io,
            session_id=session.id,
            # 01a068ea: seed past the row's existing history. The default
            # start_seq=0 landed this record at seq=1, silently OVERWRITING
            # whatever real message already held that seq on any session
            # that errors out after messages exist (pool.py:596's pattern).
            start_seq=session.last_seq,
        )
        new_seq = await writer.append(record)
        await writer.flush()
        # 01a068ea: a durable-but-unticked write is invisible to a live
        # client until its next poll -- same advisory doctrine as every
        # other tick publish in this codebase (best-effort, never fails
        # the write it's reporting on).
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    f"session:{session.id}:tick", {"seq": new_seq},
                )
            except Exception:  # noqa: BLE001 - advisory
                logger.exception(
                    "on_release: tick publish failed for session %s",
                    session.id,
                )
