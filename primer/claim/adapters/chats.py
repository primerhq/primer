from __future__ import annotations

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.int.storage import Storage


class ChatClaimAdapter(ClaimAdapter):
    kind = ClaimKind.CHAT
    entity_table = "chats"

    def __init__(self, *, chat_storage: Storage | None) -> None:
        self._storage = chat_storage

    def eligibility_sql(self) -> str:
        return (
            "e.data->>'status' = 'active' "
            "AND e.data->>'parked_status' IS NULL "
            "AND e.data->>'turn_status' IN ('claimable','resumable')"
        )

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        if self._storage is None:
            raise RuntimeError(
                "chat_storage is None — cannot run on_release without a storage backend"
            )
        chat = await self._storage.get(entity_id)
        if chat is None:
            return

        # 'idle' when the lease is fully done; 'claimable' if more work pending.
        next_status = "idle" if (outcome.success and outcome.drop_lease) else "claimable"
        updated = chat.model_copy(update={"turn_status": next_status})
        await self._storage.update(updated)
