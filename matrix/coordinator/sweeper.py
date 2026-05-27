"""Coordinator sweeper — cleans expired rate-limit and leader leases.

Runs as an elected background task (role: ``ROLE_COORDINATOR_SWEEPER``)
so exactly one instance per cluster performs the cleanup. Sweep cadence
is fixed at 30 seconds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from matrix.bus.scheduler_tasks import _BackgroundTask
from matrix.int.coordinator import ROLE_COORDINATOR_SWEEPER

if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 30.0


async def sweep_expired_leases(storage_provider: "StorageProvider") -> int:
    """Delete every lease whose ``expires_at < now()``. Returns the
    number of rows removed (sum across both lease tables)."""
    deleted = 0
    async with storage_provider.pool.acquire() as conn:
        r1 = await conn.execute(
            "DELETE FROM rate_limit_lease WHERE expires_at < now()",
        )
        # asyncpg returns "DELETE <n>" as a string
        try:
            deleted += int(r1.split()[1])
        except (IndexError, ValueError):
            pass
        r2 = await conn.execute(
            "DELETE FROM leader_lease WHERE expires_at < now()",
        )
        try:
            deleted += int(r2.split()[1])
        except (IndexError, ValueError):
            pass
    return deleted


class CoordinatorSweeper(_BackgroundTask):
    role = ROLE_COORDINATOR_SWEEPER

    def __init__(
        self,
        *,
        storage_provider: "StorageProvider",
        poll_seconds: float = _SWEEP_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(name="coordinator-sweeper")
        self._storage = storage_provider
        self._poll = poll_seconds

    async def _run(self) -> None:
        while not self._stopping:
            try:
                n = await sweep_expired_leases(self._storage)
                if n:
                    logger.info(
                        "coordinator-sweeper reclaimed %d leases", n,
                    )
            except Exception:
                logger.exception("coordinator-sweeper tick failed")
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                return
