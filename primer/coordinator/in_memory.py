"""In-memory backends for the Coordinator primitives.

Used in single-process mode. Each backend is process-local; nothing is
durable. Distributed mode uses the Postgres backends in
:mod:`primer.coordinator.postgres`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from primer.int.coordinator import (
    InvalidationBus,
    InvalidationSubscription,
    InvalidationTopic,
    LeaderElector,
    LeadershipLease,
    RateLimiter,
    RateLimiterLease,
)


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
        except TimeoutError:
            return None
        return _InMemoryRateLimiterLease(sem)


# ---------------------------------------------------------------------------
# InvalidationBus
# ---------------------------------------------------------------------------


class _InMemoryInvalidationSubscription(InvalidationSubscription):
    def __init__(
        self,
        bus: "InMemoryInvalidationBus",
        topic: InvalidationTopic,
        handler: Callable[[str], Awaitable[None]],
    ) -> None:
        self._bus = bus
        self._topic = topic
        self._handler = handler
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self._topic, self._handler)


class InMemoryInvalidationBus(InvalidationBus):
    """Process-local pub/sub for cache invalidations.

    Single-process: handlers fire synchronously inside :meth:`publish`.
    Cross-process is a no-op (there are no other processes). Subscriber
    handlers that raise are caught + logged; other subscribers still
    fire.
    """

    def __init__(self) -> None:
        self._handlers: dict[
            InvalidationTopic, list[Callable[[str], Awaitable[None]]]
        ] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def publish(self, topic: InvalidationTopic, key: str) -> None:
        async with self._lock:
            handlers = list(self._handlers.get(topic, ()))
        for handler in handlers:
            try:
                await handler(key)
            except Exception:
                logger.exception(
                    "invalidation handler raised; continuing",
                )

    async def subscribe(
        self,
        topic: InvalidationTopic,
        handler: Callable[[str], Awaitable[None]],
        *,
        on_reconnect: Callable[[], None] | None = None,
    ) -> InvalidationSubscription:
        # In-process pub/sub has no droppable transport, so it never
        # reconnects and never fires ``on_reconnect``. Accepted only to
        # satisfy the InvalidationBus contract.
        del on_reconnect
        async with self._lock:
            self._handlers[topic].append(handler)
        return _InMemoryInvalidationSubscription(self, topic, handler)

    def _unsubscribe(
        self,
        topic: InvalidationTopic,
        handler: Callable[[str], Awaitable[None]],
    ) -> None:
        try:
            self._handlers[topic].remove(handler)
        except (ValueError, KeyError):
            pass


# ---------------------------------------------------------------------------
# LeaderElector
# ---------------------------------------------------------------------------


class _InMemoryLeadershipLease(LeadershipLease):
    """Trivial lease: in-memory backend means single process == leader."""

    def __init__(self, role: str) -> None:
        super().__init__(role=role, owner_id="local", lost_event=asyncio.Event())
        self._released = False

    async def release(self) -> None:
        self._released = True


class InMemoryLeaderElector(LeaderElector):
    """Single-process backend: every ``try_acquire`` succeeds.

    The ``lost_event`` never fires because there's no other process to
    steal the role. Background tasks supervised by this elector run
    once and never need to relinquish leadership.
    """

    async def try_acquire(
        self, role: str, *, lease_seconds: int = 30,
    ) -> LeadershipLease | None:
        return _InMemoryLeadershipLease(role)
