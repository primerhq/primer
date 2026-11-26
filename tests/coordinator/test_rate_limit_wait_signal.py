"""1.5: a limiter tells the caller's task when a slot wait starts and ends (the worker pool uses it, see tests/worker/test_review_h3_rate_wait_slots.py).

Both limiters bracket ONLY the blocking wait: an acquire that is admitted at once never signals, a cancelled wait ends its signal, and with no hook set
(any caller outside a worker turn) nothing changes.
"""

from __future__ import annotations

import asyncio

import pytest

import primer.coordinator.postgres as pg
from primer.bus.in_memory import InMemoryEventBus
from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.coordinator.postgres import PostgresRateLimiter
from primer.coordinator.wait_signal import rate_limit_wait_hook


class _PgLimiter(PostgresRateLimiter):
    """Postgres's admit decision stands in for a counter, so the real queue and poller run without a database."""

    def __init__(self, bus):
        self._bus = bus
        self.held = 0
        self._queues: dict = {}

    async def _try_insert(self, key, max_concurrency):
        await asyncio.sleep(0)
        if self.held >= max_concurrency:
            return None
        self.held += 1
        limiter = self

        class _Lease:
            async def release(self_inner):
                limiter.held -= 1
                await limiter._announce_release(key)

        return _Lease()


def _in_memory():
    return InMemoryRateLimiter()


def _postgres(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    return _PgLimiter(InMemoryEventBus())


@pytest.fixture(params=["in_memory", "postgres"])
def limiter(request, monkeypatch):
    return _in_memory() if request.param == "in_memory" else _postgres(monkeypatch)


async def _in_hook_task(events, coro_fn):
    """Run ``coro_fn`` in a task whose context carries a hook that records its signals."""
    rate_limit_wait_hook.set(events.append)
    return await coro_fn()


@pytest.mark.asyncio
async def test_a_wait_is_bracketed_and_an_immediate_admission_is_not(limiter):
    events: list[bool] = []
    fast = asyncio.create_task(_in_hook_task(events, lambda: limiter.acquire("k", max_concurrency=1)))
    first = await asyncio.wait_for(fast, 2.0)
    assert events == [], "an acquire admitted at once is not a wait"

    waiting_events: list[bool] = []
    slow = asyncio.create_task(_in_hook_task(waiting_events, lambda: limiter.acquire("k", max_concurrency=1)))
    await asyncio.sleep(0.2)
    assert not slow.done()
    assert waiting_events == [True], "the wait has started and not ended"
    await first.release()
    second = await asyncio.wait_for(slow, 5.0)
    assert waiting_events == [True, False], "the grant ends the wait"
    await second.release()


@pytest.mark.asyncio
async def test_a_cancelled_wait_ends_its_signal(limiter):
    held = await limiter.acquire("k", max_concurrency=1)
    events: list[bool] = []
    slow = asyncio.create_task(_in_hook_task(events, lambda: limiter.acquire("k", max_concurrency=1)))
    await asyncio.sleep(0.2)
    assert events == [True]
    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow
    assert events == [True, False]
    await held.release()


@pytest.mark.asyncio
async def test_no_hook_changes_nothing(limiter):
    held = await limiter.acquire("k", max_concurrency=1)
    slow = asyncio.create_task(limiter.acquire("k", max_concurrency=1))  # no hook in this context
    await asyncio.sleep(0.1)
    await held.release()
    lease = await asyncio.wait_for(slow, 5.0)
    await lease.release()


@pytest.mark.asyncio
async def test_a_hook_that_raises_does_not_break_the_acquire(limiter):
    def boom(_waiting: bool) -> None:
        raise RuntimeError("hook bug")

    async def acquire_with_bad_hook():
        rate_limit_wait_hook.set(boom)
        return await limiter.acquire("k", max_concurrency=1)

    held = await limiter.acquire("k", max_concurrency=1)
    slow = asyncio.create_task(acquire_with_bad_hook())
    await asyncio.sleep(0.1)
    await held.release()
    lease = await asyncio.wait_for(slow, 5.0)
    await lease.release()
