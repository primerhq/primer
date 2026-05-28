"""Tests for matrix.coordinator.in_memory.InMemoryRateLimiter."""

from __future__ import annotations

import asyncio

import pytest

from primer.coordinator.in_memory import InMemoryRateLimiter


@pytest.mark.asyncio
async def test_acquire_releases_on_context_exit():
    rl = InMemoryRateLimiter()
    async with await rl.acquire("k", max_concurrency=1):
        pass
    async with await rl.acquire("k", max_concurrency=1):
        pass


@pytest.mark.asyncio
async def test_acquire_blocks_when_at_limit():
    rl = InMemoryRateLimiter()
    lease1 = await rl.acquire("k", max_concurrency=1)
    holder = asyncio.create_task(rl.acquire("k", max_concurrency=1))
    await asyncio.sleep(0.05)
    assert not holder.done()
    await lease1.release()
    lease2 = await asyncio.wait_for(holder, timeout=1.0)
    await lease2.release()


@pytest.mark.asyncio
async def test_try_acquire_returns_none_on_timeout():
    rl = InMemoryRateLimiter()
    await rl.acquire("k", max_concurrency=1)
    result = await rl.try_acquire("k", max_concurrency=1, timeout_s=0.05)
    assert result is None


@pytest.mark.asyncio
async def test_keys_are_independent():
    rl = InMemoryRateLimiter()
    await rl.acquire("a", max_concurrency=1)
    lease_b = await asyncio.wait_for(
        rl.acquire("b", max_concurrency=1), timeout=0.5,
    )
    await lease_b.release()


@pytest.mark.asyncio
async def test_max_concurrency_change_swaps_semaphore():
    rl = InMemoryRateLimiter()
    l1 = await rl.acquire("k", max_concurrency=1)
    l2 = await asyncio.wait_for(
        rl.acquire("k", max_concurrency=2), timeout=0.5,
    )
    await l1.release()
    await l2.release()


@pytest.mark.asyncio
async def test_heartbeat_is_a_noop():
    """In-memory backend has no lease store; heartbeat just confirms the
    lease object hasn't been released yet."""
    rl = InMemoryRateLimiter()
    lease = await rl.acquire("k", max_concurrency=1)
    assert await lease.heartbeat() is True
    await lease.release()
