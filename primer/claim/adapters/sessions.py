from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.int.storage import Storage

if TYPE_CHECKING:
    from primer.session.persistence import WorkspaceIO


class SessionClaimAdapter(ClaimAdapter):
    kind = ClaimKind.SESSION
    entity_table = "sessions"

    def __init__(
        self,
        *,
        session_storage: Storage | None,
        workspace_io: "WorkspaceIO | None" = None,
    ) -> None:
        self._storage = session_storage
        self._workspace_io = workspace_io

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
                "parked_until": p.parked_until,
                "parked_at": p.parked_at,
                "parked_state": p.parked_state,
                "last_worker_id": None,
            })
            await self._storage.update(parked, conn=conn)
            return

        # Non-park release: clear any park columns. Only bump turn_no /
        # stamp last_turn_at when a turn actually ran (outcome.success).
        # A failed release (reclaim, executor build failure, executor crash)
        # must leave the counters untouched so the next claim sees the same
        # turn.
        updates: dict[str, object | None] = {
            "parked_status": None,
            "parked_event_key": None,
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
        if not outcome.success and self._workspace_io is not None:
            await self._write_terminal_record(entity_id, outcome)

    async def _write_terminal_record(
        self, session_id: str, outcome: ReleaseOutcome
    ) -> None:
        """Append a synthetic error-kind SessionMessageRecord to messages.jsonl."""
        from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
        from primer.session.persistence import WorkspaceMessageWriter

        reason = outcome.last_error or "unknown"
        record = SessionMessageRecord(
            seq=1,  # WorkspaceMessageWriter overwrites this
            kind=SessionMessageKind.ERROR,
            payload={"reason": reason, "terminal": True},
            created_at=datetime.now(timezone.utc),
        )
        writer = WorkspaceMessageWriter(
            workspace_io=self._workspace_io,
            session_id=session_id,
        )
        await writer.append(record)
        await writer.flush()
