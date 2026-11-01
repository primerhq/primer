"""M10: a waiter at the limit wakes on its key's release announcement, not on a 0.1 s poll; the 1 s poll is only the fallback."""

from __future__ import annotations

import asyncio

import pytest

import primer.coordinator.postgres as pg
from primer.bus.in_memory import InMemoryEventBus
from primer.coordinator.postgres import PostgresRateLimiter


class _Limiter(PostgresRateLimiter):
    """The admit decision is Postgres's; here a counter stands in so the waiting logic can be driven without a database."""

    def __init__(self, bus, limit_state):
        self._bus = bus
        self.state = limit_state  # {"held": n}
        self.attempts = 0

    async def _try_insert(self, key, max_concurrency):
        self.attempts += 1
        if self.state["held"] >= max_concurrency:
            return None
        self.state["held"] += 1
        limiter = self

        class _Lease:
            async def release(self_inner):
                limiter.state["held"] -= 1
                await limiter._announce_release(key)

        return _Lease()


async def test_a_waiter_wakes_on_the_release_not_on_a_poll(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)  # so only an announcement can wake it inside the test
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 0})
    first = await limiter.acquire("k", max_concurrency=1)
    waiter = asyncio.create_task(limiter.acquire("k", max_concurrency=1))
    await asyncio.sleep(0.3)
    assert not waiter.done()
    attempts_while_blocked = limiter.attempts
    assert attempts_while_blocked <= 2  # one for the first holder, one for the waiter: it is not polling
    await first.release()
    second = await asyncio.wait_for(waiter, 1.0)
    assert second is not None and limiter.attempts == attempts_while_blocked + 1


async def test_a_release_of_another_key_does_not_wake_the_waiter(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 0})
    await limiter.acquire("k", max_concurrency=1)
    waiter = asyncio.create_task(limiter.acquire("k", max_concurrency=1))
    await asyncio.sleep(0.1)
    before = limiter.attempts
    await limiter._announce_release("other")
    await asyncio.sleep(0.1)
    assert limiter.attempts == before and not waiter.done()
    waiter.cancel()


async def test_a_lost_announcement_is_found_by_the_fallback_poll(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 0.1)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 1})
    waiter = asyncio.create_task(limiter.acquire("k", max_concurrency=1))
    await asyncio.sleep(0.15)
    limiter.state["held"] = 0  # freed without an announcement
    assert await asyncio.wait_for(waiter, 1.0) is not None


async def test_try_acquire_times_out_and_without_a_bus_still_polls():
    limiter = _Limiter(None, {"held": 1})
    assert await limiter.try_acquire("k", max_concurrency=1, timeout_s=0.25) is None
    assert limiter.attempts >= 2  # polled at 0.1 s
