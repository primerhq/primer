from matrix.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome


class ChatClaimAdapter(ClaimAdapter):
    kind = ClaimKind.CHAT
    entity_table = "chats"

    def __init__(self, *, chat_storage) -> None:
        self._storage = chat_storage

    def eligibility_sql(self) -> str:
        return (
            "e.data->>'status' = 'active' "
            "AND e.data->>'parked_status' IS NULL "
            "AND e.data->>'turn_status' IN ('claimable','resumable')"
        )

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        # Set turn_status to 'idle' (success) or 'claimable' (more user input pending)
        next_status = "idle" if outcome.success and outcome.drop_lease else "claimable"
        await self._storage.set_turn_status(entity_id, next_status)
