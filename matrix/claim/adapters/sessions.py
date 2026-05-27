from __future__ import annotations

from matrix.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from matrix.int.storage import Storage


class SessionClaimAdapter(ClaimAdapter):
    kind = ClaimKind.SESSION
    entity_table = "sessions"

    def __init__(self, *, session_storage: Storage | None) -> None:
        self._storage = session_storage

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
