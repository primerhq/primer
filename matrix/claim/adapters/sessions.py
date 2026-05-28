from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from matrix.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from matrix.int.storage import Storage

if TYPE_CHECKING:
    from matrix.session.persistence import WorkspaceIO


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
        return "e.parked_status IS NULL"

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        if self._storage is None:
            raise RuntimeError(
                "session_storage is None — cannot run on_release without a storage backend"
            )
        sess = await self._storage.get(entity_id)
        if sess is None:
            return

        # Bump turn counter and clear all park fields.
        updated = sess.model_copy(update={
            "turn_no": sess.turn_no + 1,
            "parked_status": None,
            "parked_event_key": None,
            "parked_until": None,
            "parked_at": None,
            "parked_state": None,
            "last_worker_id": None,
        })
        await self._storage.update(updated)

        # Write a terminal error record to messages.jsonl when the release
        # is a failure (reclaim, worker crash, or any other engine error).
        if not outcome.success and self._workspace_io is not None:
            await self._write_terminal_record(entity_id, outcome)

    async def _write_terminal_record(
        self, session_id: str, outcome: ReleaseOutcome
    ) -> None:
        """Append a synthetic error-kind SessionMessageRecord to messages.jsonl."""
        from matrix.model.workspace_session import SessionMessageKind, SessionMessageRecord
        from matrix.session.persistence import WorkspaceMessageWriter

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
