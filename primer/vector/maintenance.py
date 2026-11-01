"""The leader-run task that drives ``VectorStoreProvider.maintain_indexes`` (GI-28).

A pgvector provider's ``reindex_cron`` says when its HNSW indexes are vacuumed, analyzed and reindexed; before this task nothing read it. A Lance provider
has no such setting, but ``optimize()`` (compaction plus old-version cleanup) runs only inside ``maintain_indexes``, and every chunk write adds a table
version, so it is maintained nightly by default. Only the leader runs it (the supervisor holds the ``vector-maintenance`` role), so one process maintains
a shared Postgres.

The task keeps no schedule of its own on disk: a provider's next fire is computed from the moment the task first sees it (and from each run), so a restart
waits for the next cron time rather than catching up.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.bus.scheduler_tasks import _BackgroundTask
from primer.int.coordinator import ROLE_VECTOR_MAINTENANCE
from primer.model.storage import OffsetPage

if TYPE_CHECKING:
    from primer.api.registries.semantic_search_registry import SemanticSearchRegistry
    from primer.int.storage import Storage

logger = logging.getLogger(__name__)

#: A Lance provider has no ``reindex_cron``; this is its schedule (nightly, off-peak).
LANCE_DEFAULT_CRON = "0 3 * * *"
DEFAULT_POLL_SECONDS = 60.0
_PAGE = 200


def cron_of(row: Any) -> str | None:
    """The schedule of one semantic-search provider row: its ``reindex_cron``, the Lance default, or ``None`` (never maintained)."""
    configured = getattr(row.config, "reindex_cron", None)
    if configured:
        return configured
    return LANCE_DEFAULT_CRON if row.provider.value == "lance" else None


class VectorMaintenance(_BackgroundTask):
    role = ROLE_VECTOR_MAINTENANCE

    def __init__(
        self,
        *,
        storage: "Storage",
        registry: "SemanticSearchRegistry",
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        super().__init__(name="vector-maintenance")
        self._storage = storage
        self._registry = registry
        self._poll = poll_seconds
        self._clock = clock
        # ssp id -> (cron it was scheduled under, next fire)
        self._next: dict[str, tuple[str, datetime]] = {}

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.run_due()
            except Exception:  # noqa: BLE001 - one bad pass must not end the task
                logger.exception("vector-maintenance: pass failed")
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _providers(self) -> list[Any]:
        rows: list[Any] = []
        while True:
            page = await self._storage.list(OffsetPage(offset=len(rows), length=_PAGE, want_total=False))
            rows.extend(page.items)
            if len(page.items) < _PAGE:
                return rows

    async def run_due(self) -> int:
        """Run ``maintain_indexes`` for every provider whose next fire has passed. Returns how many ran."""
        from croniter import croniter

        now = self._clock()
        rows = await self._providers()
        live = {row.id for row in rows}
        for gone in set(self._next) - live:
            del self._next[gone]
        ran = 0
        for row in rows:
            cron = cron_of(row)
            if cron is None:
                self._next.pop(row.id, None)
                continue
            scheduled = self._next.get(row.id)
            if scheduled is None or scheduled[0] != cron:  # first sight, or the schedule was edited
                self._next[row.id] = (cron, croniter(cron, now).get_next(datetime))
                continue
            if now < scheduled[1]:
                continue
            self._next[row.id] = (cron, croniter(cron, now).get_next(datetime))
            try:
                provider = await self._registry.get_provider(row.id)
                reports = await provider.maintain_indexes()
            except Exception:  # noqa: BLE001 - one provider's failure must not skip the others
                logger.exception("vector-maintenance: %s failed", row.id)
                continue
            ran += 1
            for report in reports:
                logger.info("vector-maintenance: %s %s: %s", row.id, report.collection_id, report.action)
        return ran


__all__ = ["LANCE_DEFAULT_CRON", "VectorMaintenance", "cron_of"]
