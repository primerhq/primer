"""Tests for primer.coordinator.in_memory.InMemoryRateLimiter."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.int.coordinator import RateLimiterLease


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
async def test_larger_limit_admits_another_holder():
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


class _Acquisitions:
    """Keep failed assertions from leaving live tasks or occupied slots."""

    def __init__(self):
        self.limiter = InMemoryRateLimiter()
        self.leases: list[RateLimiterLease] = []
        self.tasks: list[asyncio.Task] = []

    async def acquire(self, limit, key="k"):
        lease = await self.limiter.acquire(key, max_concurrency=limit)
        self.leases.append(lease)
        return lease

    async def try_acquire(self, limit, *, key="k", timeout=0.01):
        lease = await self.limiter.try_acquire(key, max_concurrency=limit, timeout_s=timeout)
        if lease is not None:
            self.leases.append(lease)
        return lease

    async def start_waiter(self, limit, *, timed=False):
        started = asyncio.Event()

        async def acquire():
            started.set()
            return await self.try_acquire(limit, timeout=10) if timed else await self.acquire(limit)

        task = asyncio.create_task(acquire())
        self.tasks.append(task)
        await started.wait()
        assert not task.done(), "the existing holders must prevent admission"
        return task


@pytest_asyncio.fixture
async def acquisitions():
    current = _Acquisitions()
    try:
        yield current
    finally:
        for task in current.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*current.tasks, return_exceptions=True)
        for lease in current.leases:
            await lease.release()


@pytest.mark.parametrize("limits,probe", [((1, 2), 2), ((3, 2, 3), 3), ((1, 4, 3, 5), 4)])
async def test_changed_limits_count_every_existing_holder(acquisitions, limits, probe):
    leases = [await acquisitions.acquire(limit) for limit in limits]
    assert await acquisitions.try_acquire(probe) is None
    await leases[0].release()
    assert await acquisitions.try_acquire(probe) is not None


async def test_smaller_limit_waits_for_all_existing_holders(acquisitions):
    first = await acquisitions.acquire(2)
    second = await acquisitions.acquire(2)
    waiter = await acquisitions.start_waiter(1)
    await first.release()
    assert await acquisitions.try_acquire(1) is None
    assert not waiter.done()
    await second.release()
    assert await asyncio.wait_for(waiter, 1) is not None


async def test_each_caller_keeps_its_own_limit(acquisitions):
    first = await acquisitions.acquire(2)
    low = await acquisitions.start_waiter(1)
    second = await acquisitions.acquire(2)
    assert not low.done()
    assert await acquisitions.try_acquire(2) is None
    await first.release()
    assert not low.done()
    await second.release()
    assert await asyncio.wait_for(low, 1) is not None


async def test_equal_limit_waiters_keep_arrival_order(acquisitions):
    first = await acquisitions.acquire(1)
    waiters = [await acquisitions.start_waiter(1) for _ in range(3)]
    await first.release()
    for index, waiter in enumerate(waiters):
        lease = await asyncio.wait_for(waiter, 1)
        assert all(not later.done() for later in waiters[index + 1:])
        await lease.release()


async def test_ineligible_waiter_does_not_block_eligible_fifo(acquisitions):
    first = await acquisitions.acquire(2)
    second = await acquisitions.acquire(2)
    low = await acquisitions.start_waiter(1)
    high_first = await acquisitions.start_waiter(2)
    high_second = await acquisitions.start_waiter(2)
    await first.release()
    third = await asyncio.wait_for(high_first, 1)
    assert not low.done() and not high_second.done()
    # The eligible waiter owns the released slot before another caller can barge.
    assert await acquisitions.try_acquire(2) is None
    await second.release()
    fourth = await asyncio.wait_for(high_second, 1)
    assert not low.done()
    await third.release()
    assert not low.done()
    await fourth.release()
    assert await asyncio.wait_for(low, 1) is not None


@pytest.mark.parametrize("timed", [False, True])
@pytest.mark.parametrize("after_grant", [False, True])
async def test_cancelled_waiter_preserves_exact_slot_count(acquisitions, timed, after_grant):
    first = await acquisitions.acquire(1)
    cancelled = await acquisitions.start_waiter(1, timed=timed)
    next_waiter = await acquisitions.start_waiter(1)
    if after_grant:
        await first.release()
    cancelled.cancel()
    cancelled.cancel()
    if not after_grant:
        # A cancelled future can still be in the wait queue until its task resumes.
        await first.release()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    replacement = await asyncio.wait_for(next_waiter, 1)
    await first.release()  # an old lease cannot release its slot twice
    assert await acquisitions.try_acquire(1) is None
    await replacement.release()
    assert await acquisitions.try_acquire(1) is not None


async def test_timeout_removes_waiter_without_freeing_a_holder(acquisitions):
    first = await acquisitions.acquire(1)
    assert await acquisitions.try_acquire(1) is None
    waiter = await acquisitions.start_waiter(1)
    await first.release()
    replacement = await asyncio.wait_for(waiter, 1)
    assert await acquisitions.try_acquire(1) is None
    await replacement.release()
    assert await acquisitions.try_acquire(1) is not None


async def test_cancelled_context_holder_releases_for_waiter(acquisitions):
    entered = asyncio.Event()

    async def hold():
        async with await acquisitions.acquire(1):
            entered.set()
            await asyncio.Event().wait()

    holder = asyncio.create_task(hold())
    acquisitions.tasks.append(holder)
    await entered.wait()
    waiter = await acquisitions.start_waiter(1)
    holder.cancel()
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    replacement = await asyncio.wait_for(waiter, 1)
    assert await acquisitions.try_acquire(1) is None
    await replacement.release()


async def test_release_cannot_suspend_between_returning_and_granting_slot(acquisitions):
    first = await acquisitions.acquire(1)
    waiter = await acquisitions.start_waiter(1)
    release = first.release()
    with pytest.raises(StopIteration):
        release.send(None)
    assert await asyncio.wait_for(waiter, 1) is not None


async def test_limit_changes_do_not_affect_another_key(acquisitions):
    first = await acquisitions.acquire(1)
    independent = await acquisitions.acquire(1, key="other")
    second = await acquisitions.acquire(2)
    assert await acquisitions.try_acquire(1, key="other") is None
    await first.release()
    await second.release()
    assert await acquisitions.try_acquire(1, key="other") is None
    await independent.release()
    assert await acquisitions.try_acquire(1, key="other") is not None


@pytest.mark.parametrize("timeout", [0, -1])
async def test_nonpositive_timeout_keeps_existing_behavior(acquisitions, timeout):
    assert await acquisitions.try_acquire(1, timeout=timeout) is None
    assert await acquisitions.acquire(1) is not None


async def test_zero_limit_wait_can_be_cancelled_then_higher_limit_admits(acquisitions):
    waiter = await acquisitions.start_waiter(0)
    assert await acquisitions.try_acquire(0) is None
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert await acquisitions.acquire(1) is not None


async def test_negative_limit_is_refused_without_affecting_holders(acquisitions):
    first = await acquisitions.acquire(1)
    with pytest.raises(ValueError):
        await acquisitions.acquire(-1)
    with pytest.raises(ValueError):
        await acquisitions.try_acquire(-1)
    assert await acquisitions.try_acquire(1) is None
    await first.release()
    assert await acquisitions.try_acquire(1) is not None
