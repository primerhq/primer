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

from primer.coordinator.wait_signal import waiting_for_slot
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


class _RateLimitState:
    """One key's holders and pending admissions, owned by one event loop."""

    def __init__(self) -> None:
        self.in_use = 0
        self.waiters: list[tuple[int, asyncio.Future[None]]] = []

    def wake_waiters(self) -> None:
        remaining = []
        for limit, waiter in self.waiters:
            if waiter.done():
                continue
            if self.in_use < limit:
                # Count the grant before scheduling its task. A new caller
                # cannot take this slot while the waiter is still waking.
                self.in_use += 1
                waiter.set_result(None)
            else:
                remaining.append((limit, waiter))
        self.waiters = remaining


class _InMemoryRateLimiterLease(RateLimiterLease):
    def __init__(self, state: _RateLimitState) -> None:
        self._state = state
        self._released = False

    async def __aenter__(self) -> "_InMemoryRateLimiterLease":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        # No suspension: even repeated cancellation cannot interrupt cleanup
        # between returning this slot and granting it to an eligible waiter.
        self._state.in_use -= 1
        self._state.wake_waiters()

    async def heartbeat(self) -> bool:
        # In-memory leases never expire. They stay alive until released.
        return not self._released


class InMemoryRateLimiter(RateLimiter):
    """Count every holder of a key across per-caller limit changes.

    Each admission uses its caller's limit, like the Postgres limiter. A
    lower limit does not preempt holders; that caller waits for occupancy
    to fall below its limit. Eligible waiters receive slots in arrival
    order, skipping earlier waiters whose own limits prohibit admission.
    This state is process-local and must stay on one event loop, like
    asyncio synchronization primitives.
    """

    def __init__(self) -> None:
        self._states: dict[str, _RateLimitState] = {}

    async def acquire(
        self, key: str, *, max_concurrency: int,
    ) -> RateLimiterLease:
        if max_concurrency < 0:
            raise ValueError("max_concurrency must not be negative")
        state = self._states.setdefault(key, _RateLimitState())
        if state.in_use < max_concurrency:
            state.in_use += 1
            return _InMemoryRateLimiterLease(state)
        waiter = asyncio.get_running_loop().create_future()
        entry = (max_concurrency, waiter)
        # No suspension between checking occupancy and registering the wait:
        # a release on this event loop cannot be missed.
        state.waiters.append(entry)
        try:
            with waiting_for_slot():
                await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                # The grant already counted us, but cancellation won before
                # acquire returned its lease. Hand the reserved slot back.
                state.in_use -= 1
                state.wake_waiters()
            elif entry in state.waiters:
                state.waiters.remove(entry)
            raise
        return _InMemoryRateLimiterLease(state)

    async def try_acquire(
        self, key: str, *, max_concurrency: int, timeout_s: float,
    ) -> RateLimiterLease | None:
        if max_concurrency < 0:
            raise ValueError("max_concurrency must not be negative")
        if timeout_s <= 0:
            return None
        try:
            async with asyncio.timeout(timeout_s):
                return await self.acquire(key, max_concurrency=max_concurrency)
        except TimeoutError:
            return None


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
