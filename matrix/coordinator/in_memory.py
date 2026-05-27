"""In-memory backends for the Coordinator primitives.

Used in single-process mode. Each backend is process-local; nothing is
durable. Distributed mode uses the Postgres backends in
:mod:`matrix.coordinator.postgres`.
"""

from __future__ import annotations

import asyncio
import logging

from matrix.int.coordinator import RateLimiter, RateLimiterLease


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class _InMemoryRateLimiterLease(RateLimiterLease):
    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._released = False

    async def __aenter__(self) -> "_InMemoryRateLimiterLease":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()

    async def heartbeat(self) -> bool:
        # In-memory leases never expire; only the in-process semaphore
        # can revoke them, which it doesn't. Always alive while held.
        return not self._released


class InMemoryRateLimiter(RateLimiter):
    """Per-key ``asyncio.Semaphore`` cache.

    When ``max_concurrency`` changes between calls for the same key the
    semaphore is replaced. Brief over-grant during the swap is accepted.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._semaphores: dict[str, tuple[asyncio.Semaphore, int]] = {}

    async def _get_semaphore(
        self, key: str, max_concurrency: int,
    ) -> asyncio.Semaphore:
        async with self._lock:
            cached = self._semaphores.get(key)
            if cached is not None and cached[1] == max_concurrency:
                return cached[0]
            sem = asyncio.Semaphore(max_concurrency)
            self._semaphores[key] = (sem, max_concurrency)
            return sem

    async def acquire(
        self, key: str, *, max_concurrency: int,
    ) -> RateLimiterLease:
        sem = await self._get_semaphore(key, max_concurrency)
        await sem.acquire()
        return _InMemoryRateLimiterLease(sem)

    async def try_acquire(
        self, key: str, *, max_concurrency: int, timeout_s: float,
    ) -> RateLimiterLease | None:
        sem = await self._get_semaphore(key, max_concurrency)
        try:
            await asyncio.wait_for(sem.acquire(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
        return _InMemoryRateLimiterLease(sem)
