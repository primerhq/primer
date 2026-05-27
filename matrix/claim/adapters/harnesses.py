from matrix.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome


class HarnessClaimAdapter(ClaimAdapter):
    kind = ClaimKind.HARNESS
    entity_table = "harnesses"

    def __init__(self, *, harness_storage) -> None:
        self._storage = harness_storage

    def eligibility_sql(self) -> str:
        return "e.data->>'pending_operation' IS NOT NULL"

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        await self._storage.on_release(entity_id, outcome=outcome)
