from __future__ import annotations

from datetime import datetime, UTC

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome
from primer.int.storage import Storage
from primer.model.harness import HarnessStatus


class HarnessClaimAdapter(ClaimAdapter):
    kind = ClaimKind.HARNESS
    # Match _table_name_for(Harness) ("harness"); see chats.py for why a plural
    # name breaks the Postgres claim query.
    entity_table = "harness"

    def __init__(self, *, harness_storage: Storage | None) -> None:
        self._storage = harness_storage

    def eligibility_sql(self) -> str:
        return "e.data->>'pending_operation' IS NOT NULL"

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        if self._storage is None:
            raise RuntimeError(
                "harness_storage is None — cannot run on_release without a storage backend"
            )
        harness = await self._storage.get(entity_id)
        if harness is None:
            return

        now = datetime.now(UTC)
        if outcome.success:
            new_status = HarnessStatus.READY
            last_error = None
        else:
            new_status = HarnessStatus.ERROR
            last_error = outcome.last_error

        updated = harness.model_copy(update={
            "pending_operation": None,
            "status": new_status,
            "last_operation_at": now,
            "last_operation_error": last_error,
        })
        await self._storage.update(updated)
