"""2.4: the Postgres limiter's in-process waiters are served in arrival order by ONE poller, so the number of DB admission attempts stays flat as the line grows (cross-process order is best effort)."""

from __future__ import annotations

import asyncio

import pytest

import primer.coordinator.postgres as pg
from primer.bus.in_memory import InMemoryEventBus
from primer.coordinator.postgres import PostgresRateLimiter


class _Limiter(PostgresRateLimiter):
    """The admit decision is Postgres's; here a counter stands in so the queue can be driven without a database."""

    def __init__(self, bus, limit_state):
        self._bus = bus
        self.state = limit_state  # {"held": n}
        self.attempts = 0
        self.order: list[int] = []
        self._queues: dict = {}

    async def _try_insert(self, key, max_concurrency):
        self.attempts += 1
        await asyncio.sleep(0)  # a real attempt awaits the database: callers arriving together interleave here
        if self.state["held"] >= max_concurrency:
            return None
        self.state["held"] += 1
        marker = len(self.order)
        self.order.append(marker)
        limiter = self

        class _Lease:
            async def release(self_inner):
                limiter.state["held"] -= 1
                await limiter._announce_release(key)

        _Lease.marker = marker
        return _Lease()


@pytest.mark.asyncio
async def test_waiters_are_admitted_in_arrival_order(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 0})
    first = await limiter.acquire("k", max_concurrency=1)
    tasks = [asyncio.create_task(limiter.acquire("k", max_concurrency=1)) for _ in range(5)]
    await asyncio.sleep(0.2)  # the line forms: each caller's arrival attempt fails and it joins, in arrival order
    assert not any(t.done() for t in tasks)
    admitted = []
    await first.release()
    for t in tasks:
        lease = await asyncio.wait_for(t, 5.0)
        assert lease is not None
        admitted.append(lease)
        await lease.release()
    assert [l.marker for l in admitted] == [1, 2, 3, 4, 5]  # arrival order, not the lock race
    # holder + 5 arrival attempts; each admission costs its own insert plus the next waiter's failed
    # insert (which finds the limit), except the last (no next waiter): 1 + 5 + 5 admits + 4 limit finds.
    assert limiter.attempts == 1 + 5 + 5 + 4


@pytest.mark.asyncio
async def test_a_long_line_keeps_the_db_load_flat(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 1})  # already at the limit
    tasks = [asyncio.create_task(limiter.acquire("k", max_concurrency=1)) for _ in range(8)]
    await asyncio.sleep(0.4)
    assert not any(t.done() for t in tasks)
    assert limiter.attempts == 8  # the callers' arrival attempts only: the line itself then makes no DB calls
    limiter.state["held"] = 0  # a release frees the slot
    await limiter._announce_release("k")
    lease = await asyncio.wait_for(tasks[0], 5.0)
    assert lease is not None
    assert limiter.attempts == 10  # 8 arrivals + the admitted waiter's insert + the next waiter's failed insert (which finds the limit)
    await lease.release()
    for t in tasks[1:]:
        t.cancel()
    await asyncio.gather(*tasks[1:], return_exceptions=True)


@pytest.mark.asyncio
async def test_a_cancelled_waiter_does_not_leak_a_slot(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 1})
    tasks = [asyncio.create_task(limiter.acquire("k", max_concurrency=1)) for _ in range(3)]
    await asyncio.sleep(0.2)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # the queue is empty and the poller is out: a new caller takes the slot on its own fast path
    limiter.state["held"] = 0
    lease = await asyncio.wait_for(limiter.acquire("k", max_concurrency=1), 5.0)
    assert lease is not None
    assert limiter.attempts == 3 + 1  # the three arrival attempts + this one, nothing queued
    await lease.release()


@pytest.mark.asyncio
async def test_callers_arriving_together_share_one_line_one_poller_and_one_attempt_per_release(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 1})  # at the limit
    tasks = [asyncio.create_task(limiter.acquire("k", max_concurrency=1)) for _ in range(8)]
    await asyncio.sleep(0.2)
    assert not any(t.done() for t in tasks)
    pollers = [t for t in asyncio.all_tasks() if t.get_name() == "rl-wait-k"]
    assert len(pollers) == 1  # one line, one poller: not one per caller
    assert len(limiter._queues["k"].waiters) == 8
    assert limiter.attempts == 8  # each caller's own fast-path attempt on arrival, nothing more
    limiter.state["held"] = 0
    await limiter._announce_release("k")
    first = await asyncio.wait_for(tasks[0], 5.0)
    assert limiter.attempts == 10  # one release costs the admitted waiter's insert + the next waiter's failed insert (finds the limit), not one per caller
    admitted = [first]
    await first.release()
    for t in tasks[1:]:
        lease = await asyncio.wait_for(t, 5.0)
        admitted.append(lease)
        await lease.release()
    assert [l.marker for l in admitted] == list(range(8))  # arrival order


@pytest.mark.asyncio
async def test_a_release_between_the_failed_attempt_and_the_first_wait_is_not_missed(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)  # only the announcement can wake the line inside the test
    bus = InMemoryEventBus()

    class _Gap(_Limiter):
        async def _try_insert(self, key, max_concurrency):
            if self.attempts == 0:
                self.attempts += 1
                # the attempt has decided "at the limit"; the holder releases before the caller joins the line
                self.state["held"] = 0
                await self._announce_release(key)
                return None
            return await super()._try_insert(key, max_concurrency)

    limiter = _Gap(bus, {"held": 1})
    lease = await asyncio.wait_for(limiter.acquire("k", max_concurrency=1), 2.0)
    assert lease is not None


@pytest.mark.asyncio
async def test_a_coalesced_release_admits_as_many_waiters_as_slots(monkeypatch):
    """One wake for several freed slots (a coalesced release) admits as many waiters as there are
    slots in a single pass, not one: the rest used to sit until the fallback poll (review 2026-11-23, G)."""
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 3})  # a cap of 3, all three held
    tasks = [asyncio.create_task(limiter.acquire("k", max_concurrency=3)) for _ in range(6)]
    await asyncio.sleep(0.2)
    assert not any(t.done() for t in tasks)
    arrivals = limiter.attempts  # the six fast-path arrival attempts
    # all three slots freed by ONE announce (a coalesced release): the pass admits three waiters,
    # not one, and the next three stay queued (the limit is reached again).
    limiter.state["held"] = 0
    await limiter._announce_release("k")
    admitted = [await asyncio.wait_for(t, 5.0) for t in tasks[:3]]
    assert all(l is not None for l in admitted)
    assert not any(t.done() for t in tasks[3:])  # the remaining waiters are still queued
    # attempts: the arrivals, then three successful admissions plus the one that finds the limit.
    assert limiter.attempts == arrivals + 3 + 1
    for l in admitted:
        await l.release()
    for t in tasks[3:]:
        t.cancel()
    await asyncio.gather(*tasks[3:], return_exceptions=True)


@pytest.mark.asyncio
async def test_a_waiter_cancelled_in_the_tick_it_is_granted_hands_the_slot_back(monkeypatch):
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 1})
    task = asyncio.create_task(limiter.acquire("k", max_concurrency=1))
    await asyncio.sleep(0.1)
    waiter = limiter._queues["k"].waiters[0]
    lease = await _grant(limiter, waiter)
    task.cancel()  # the grant landed, the cancel arrives before the task runs again
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease.released
    assert limiter.state["held"] == 0  # the slot went back, it did not leak


@pytest.mark.asyncio
async def test_a_caller_enqueuing_during_a_slow_multi_slot_pass_is_not_blocked_for_the_whole_pass(monkeypatch):
    """X1 review M1: the admission pass holds the queue lock PER ADMISSION, not across the whole
    pass: a caller enqueuing on the same key while a coalesced multi-slot pass is running its
    (slow) inserts is not blocked behind the pass for all of them, so its short-timeout
    try_acquire is admitted by the pass's remaining slot instead of lapsing while a slot was
    in fact free (the old pass held the lock for every insert, so the enqueue blocked on it)."""
    monkeypatch.setattr(pg, "_WAIT_FALLBACK_SECONDS", 30.0)
    bus = InMemoryEventBus()
    limiter = _Limiter(bus, {"held": 4})  # a cap of 4, all four held
    tasks = [asyncio.create_task(limiter.acquire("k", max_concurrency=4)) for _ in range(3)]
    await asyncio.sleep(0.2)
    assert not any(t.done() for t in tasks)
    # Slow the pass's inserts: each admission costs a real wait the old pass spent under the lock.
    inner = limiter._try_insert

    async def _slow_insert(key, max_concurrency):
        await asyncio.sleep(0.05)
        return await inner(key, max_concurrency)

    monkeypatch.setattr(limiter, "_try_insert", _slow_insert)
    arrivals = limiter.attempts  # the three fast-path arrival attempts
    limiter.state["held"] = 0
    await limiter._announce_release("k")  # coalesced release: the pass admits the three waiters

    # Wait until the pass has finished ONE of its slow inserts: it is definitely
    # mid-pass (and, on the old code, definitely holding the queue lock).
    deadline = asyncio.get_running_loop().time() + 2.0
    while limiter.attempts < arrivals + 1:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the pass did not start admitting the queued waiters")
        await asyncio.sleep(0.005)
    # A new caller on the same key joins the line while the pass is still running.
    late = await limiter.try_acquire("k", max_concurrency=4, timeout_s=1.0)
    assert late is not None, (
        "the late caller was blocked on the queue lock for the whole pass and its "
        "short timeout lapsed while a slot was in fact free: the pass must hold the "
        "lock per admission, not across all of them"
    )
    assert late.marker == 3, "the late caller is admitted after the three queued ones (arrival order)"
    for t in tasks:
        assert t.done()  # the pass admitted all three queued waiters
    await late.release()


async def _grant(limiter, waiter):
    """Resolve ``waiter`` the way the admission pass does: the insert took a slot, the future gets the lease."""
    limiter.state["held"] = 0
    lease = await limiter._try_insert("k", 1)
    assert lease is not None and limiter.state["held"] == 1
    inner_release = lease.release

    async def release():
        lease.released = True
        await inner_release()

    lease.release = release
    lease.released = False
    waiter.future.set_result(lease)
    return lease
