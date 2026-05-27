from matrix.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome


class SessionClaimAdapter(ClaimAdapter):
    kind = ClaimKind.SESSION
    entity_table = "sessions"

    def __init__(self, *, session_storage) -> None:
        self._storage = session_storage

    def eligibility_sql(self) -> str:
        return "e.parked_status IS NULL"

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        # Bump turn_no, clear parked_*, etc. Entity-storage-side.
        await self._storage.on_release(entity_id, outcome=outcome)
