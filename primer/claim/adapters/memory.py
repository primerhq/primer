"""The ``memory`` claim kind: ``MemoryJob`` rows are the claim entity table (agent memory, spec 6.1).

A job is claimable for as long as its row exists (a job whose agent or row is gone is dropped by ``run_memory_job``, which holds the gates). The row
carries no state the claim engine writes back, so ``on_release`` has nothing to do; the lease's own ``next_attempt_at`` is the debounce.
"""

from __future__ import annotations

from datetime import datetime

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome, read_for_eligibility
from primer.int.storage import Storage


class MemoryClaimAdapter(ClaimAdapter):
    kind = ClaimKind.MEMORY
    # Match _table_name_for(MemoryJob) ("memoryjob"); a different name breaks the Postgres claim query.
    entity_table = "memoryjob"

    def __init__(self, *, storage: Storage | None) -> None:
        self._storage = storage

    def eligibility_sql(self) -> str:
        return "TRUE"

    async def is_eligible(self, entity_id: str, now: datetime) -> bool:
        """Mirrors :meth:`eligibility_sql`: a missing row is not eligible (the claim query's inner join); with no storage the lease stays claimable."""
        if self._storage is None:
            return True
        return await read_for_eligibility(self._storage, entity_id) is not None

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        return None
