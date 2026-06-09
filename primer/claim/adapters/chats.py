from __future__ import annotations

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.int.storage import Storage


class ChatClaimAdapter(ClaimAdapter):
    kind = ClaimKind.CHAT
    # Must match _table_name_for(Chat) in primer.storage.postgres ("chat",
    # the lowercased class name). A plural "chats" JOINs a table that never
    # matches storage and 404s the claim query on a fresh Postgres DB.
    entity_table = "chat"

    def __init__(self, *, chat_storage: Storage | None) -> None:
        self._storage = chat_storage

    def eligibility_sql(self) -> str:
        # 'running' is included for crash recovery: the claim query only ever
        # returns a row whose lease is unclaimed OR expired
        # (``claimed_by IS NULL OR expires_at < now()``), so a 'running' chat
        # is reclaimable ONLY when its worker died/stalled past the lease TTL
        # -- a live worker keeps the lease heartbeated and is never stolen.
        # This mirrors how harnesses recover (their eligibility stays true
        # while pending_operation is set). Without it a dead worker's chat is
        # stranded at turn_status='running' forever (see FINDINGS F9).
        return (
            "e.data->>'status' = 'active' "
            "AND e.data->>'parked_status' IS NULL "
            "AND e.data->>'turn_status' IN ('claimable','resumable','running')"
        )

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        if self._storage is None:
            raise RuntimeError(
                "chat_storage is None — cannot run on_release without a storage backend"
            )
        chat = await self._storage.get(entity_id, conn=conn)
        if chat is None:
            return

        # 'idle' when the lease is fully done; 'claimable' if more work pending.
        next_status = "idle" if (outcome.success and outcome.drop_lease) else "claimable"
        updated = chat.model_copy(update={"turn_status": next_status})
        await self._storage.update(updated, conn=conn)
