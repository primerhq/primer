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
        return "e.data->>'parked_status' IS NULL"

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        if self._storage is None:
            raise RuntimeError(
                "session_storage is None — cannot run on_release without a storage backend"
            )
        sess = await self._storage.get(entity_id)
        if sess is None:
            return

        # Only bump turn_no / stamp last_turn_at when a turn actually ran.
        # A failed release (reclaim, executor build failure, executor crash)
        # must leave the counters untouched so the next claim sees the same
        # turn — otherwise a stuck row drifts to turn_no=N with no matching
        # messages.jsonl entries (the symptom in
        # docs/superpowers/specs analysis).
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
        await self._storage.update(updated)

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
