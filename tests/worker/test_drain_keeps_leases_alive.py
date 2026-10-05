"""A draining pool keeps the leases of its running turns alive until they finish (or the drain deadline passes).

`drain_and_stop` stops CLAIMING, not KEEPING. `_heartbeat_loop` used to exit within one heartbeat interval of the
drain starting, while the drain waits up to `drain_timeout_seconds` (default 120) for running turns and a lease
lives `lease_ttl_seconds` (default 30): a turn still running 30 s into a drain lost its lease, a peer's
`claim_due` took it (expired leases are eligible) and ran a DUPLICATE execution, and the draining worker never
learned of it (lost-lease detection lives in the same loop).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

KIND = ClaimKind.HARNESS


class _Adapter:
    entity_table = "spy"
    kind = KIND

    def eligibility_sql(self) -> str:
        return "true"

    async def on_release(self, conn, entity_id, *, outcome):
        return None


async def _until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


def _config() -> WorkerConfig:
    return WorkerConfig(
        concurrency=8, claim_batch_size=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5,
        poll_interval_seconds=0.1, drain_timeout_seconds=20,
    )


async def _pool(engine, scheduler, handler) -> WorkerPool:
    pool = WorkerPool(
        config=_config(), scheduler=scheduler,
        storage=None, workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
        engine=engine,
    )
    await pool.start()
    engine.lease_ttl_seconds = 4     # start() applies the config's 5 s floor; 4 s against a 1 s heartbeat leaves a 3 s CI margin
    pool._dispatch[KIND] = handler
    return pool


@pytest.mark.asyncio
async def test_a_turn_that_outlives_the_lease_ttl_during_a_drain_keeps_its_lease_and_no_peer_takes_it():
    engine = InMemoryClaimEngine(adapters={KIND: _Adapter()})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    gate = asyncio.Event()
    started_a: list[str] = []
    started_b: list[str] = []

    async def handler_a(lease):
        started_a.append(lease.entity_id)
        await gate.wait()

    async def handler_b(lease):
        started_b.append(lease.entity_id)
        await gate.wait()

    pool_a = await _pool(engine, scheduler, handler_a)
    try:
        for key in ("long-1", "long-2"):
            await engine.upsert(KIND, key)
        await _until(lambda: sorted(started_a) == ["long-1", "long-2"], "pool A never started both turns")
        before = {k: engine._leases[(KIND, k)].expires_at for k in ("long-1", "long-2")}

        drain = asyncio.create_task(pool_a.drain_and_stop(timeout=20))
        await _until(pool_a._stopping.is_set, "drain never started")
        pool_b = await _pool(engine, scheduler, handler_b)       # a peer, polling for expired leases
        try:
            await asyncio.sleep(4.6)                              # past the ORIGINAL expiry (ttl 4 s)

            for key in ("long-1", "long-2"):
                row = engine._leases[(KIND, key)]
                assert row.claimed_by == pool_a._worker_id, f"{key}: the lease left the draining worker"
                assert row.expires_at > before[key], f"{key}: nothing refreshed its lease during the drain"
                assert row.expires_at > datetime.now(UTC), f"{key}: the lease expired during the drain"
            assert started_b == [], "a peer ran a DUPLICATE execution of a turn the draining worker still runs"
        finally:
            gate.set()
            await asyncio.wait_for(drain, timeout=10.0)
            await pool_b.drain_and_stop(timeout=3)
    finally:
        gate.set()
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_the_keepalive_stops_once_the_turns_are_done_so_drain_does_not_hang_on_it():
    engine = InMemoryClaimEngine(adapters={KIND: _Adapter()})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    gate = asyncio.Event()
    started: list[str] = []

    async def handler(lease):
        started.append(lease.entity_id)
        await gate.wait()

    pool = await _pool(engine, scheduler, handler)
    try:
        for key in ("a", "b"):
            await engine.upsert(KIND, key)
        await _until(lambda: sorted(started) == ["a", "b"], "turns never started")
        loops = list(pool._tasks)       # drain_and_stop clears ``_tasks``, so the loops are captured BEFORE it
        assert loops, "the pool started no loops"
        beats_after_stopping: list[int] = []
        real_heartbeat = engine.heartbeat

        async def counting_heartbeat(worker_id, keys):
            if pool._stopping.is_set():
                beats_after_stopping.append(len(keys))
            return await real_heartbeat(worker_id, keys)

        engine.heartbeat = counting_heartbeat           # type: ignore[method-assign]
        drain = asyncio.create_task(pool.drain_and_stop(timeout=20))
        await _until(pool._stopping.is_set, "drain never started")
        # Checked only after MORE than one heartbeat interval (1 s): a loop that ended on ``_stopping`` would still
        # be asleep right after the drain began and finish at its next wake-up, so checking at once proves nothing.
        await asyncio.sleep(1.5)
        assert not any(t.done() for t in loops), "a keep-alive loop ended once the drain began"
        assert beats_after_stopping, "the lease heartbeat stopped once the drain began"
        gate.set()
        await asyncio.wait_for(drain, timeout=5.0)

        assert pool._keepalive_done.is_set()
        assert all(t.done() for t in loops), "a keep-alive loop outlived the drain"
    finally:
        gate.set()
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_the_keepalive_is_bounded_even_if_a_turn_ignores_its_cancel():
    """The absolute deadline: past it the heartbeat stops, so the worker does not keep a lease alive for ever for
    a task it cannot stop."""
    engine = InMemoryClaimEngine(adapters={KIND: _Adapter()})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = await _pool(engine, scheduler, handler=lambda lease: asyncio.sleep(3600))
    try:
        pool._stopping.set()
        pool._keepalive_deadline = asyncio.get_event_loop().time() + 0.3
        beats = []
        real = engine.heartbeat

        async def counting_heartbeat(worker_id, keys):
            beats.append(len(keys))
            return await real(worker_id, keys)

        engine.heartbeat = counting_heartbeat           # type: ignore[method-assign]
        pool._in_flight.add((KIND, "k"))
        await asyncio.sleep(2.5)                        # 2+ heartbeat intervals, well past the 0.3 s deadline
        heartbeat_task = next(t for t in pool._tasks if "scheduler-heartbeat" in t.get_name())
        assert heartbeat_task.done(), "the heartbeat loop never honoured the deadline"
    finally:
        pool._in_flight.clear()
        await pool.drain_and_stop(timeout=1)
        await scheduler.aclose()


class _SlowDrainScheduler(InMemoryScheduler):
    """``drain_worker`` records the keep-alive deadline the pool has set by then, then takes ``delay`` seconds."""

    def __init__(self, pool_ref: list, delay: float) -> None:
        super().__init__()
        self._pool_ref = pool_ref
        self._delay = delay
        self.seen_deadline: float | None = None

    async def drain_worker(self, worker_id):
        self.seen_deadline = self._pool_ref[0]._keepalive_deadline
        await asyncio.sleep(self._delay)
        return await super().drain_worker(worker_id)


@pytest.mark.asyncio
async def test_drain_and_stop_sets_the_keepalive_deadline_from_the_drain_start_and_the_timeout():
    """Pins the value ``drain_and_stop`` ACTUALLY sets (the other tests overwrite the attribute): the drain's start
    plus its timeout plus the 30 s keep-alive allowance."""
    engine = InMemoryClaimEngine(adapters={KIND: _Adapter()})
    ref: list = []
    scheduler = _SlowDrainScheduler(ref, delay=0.0)
    await scheduler.initialize()
    pool = await _pool(engine, scheduler, handler=lambda lease: asyncio.sleep(0))
    ref.append(pool)
    try:
        loop = asyncio.get_event_loop()
        before = loop.time()
        await pool.drain_and_stop(timeout=7.0)
        after = loop.time()
        assert scheduler.seen_deadline is not None
        assert before + 7.0 + 30.0 <= scheduler.seen_deadline <= after + 7.0 + 30.0
        assert pool._KEEPALIVE_EXTRA_SECONDS == 30.0
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_slow_drain_worker_cannot_push_the_turn_wait_past_the_drains_own_clock():
    """One clock for the keep-alive and the turn wait: with ``drain_worker`` taking 3 s and a 2 s drain timeout the turn wait
    used to START after it (so the cancel arrived at about 5 s); it is capped at the drain's start + timeout + two
    ``_stop_claiming`` waits, so the cancel arrives at about 3 s and the keep-alive (started + timeout + 30 s) still covers
    the unwind."""
    engine = InMemoryClaimEngine(adapters={KIND: _Adapter()})
    ref: list = []
    scheduler = _SlowDrainScheduler(ref, delay=3.0)
    await scheduler.initialize()
    cancelled_at: list[float] = []
    started: list[str] = []
    loop = asyncio.get_event_loop()

    async def handler(lease):
        started.append(lease.entity_id)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_at.append(loop.time())
            raise

    pool = await _pool(engine, scheduler, handler)
    ref.append(pool)
    pool._claim_stop_grace_seconds = 0.2
    try:
        for key in ("a", "b"):
            await engine.upsert(KIND, key)
        await _until(lambda: sorted(started) == ["a", "b"], "turns never started")
        t0 = loop.time()
        await asyncio.wait_for(pool.drain_and_stop(timeout=2.0), timeout=15.0)
        assert len(cancelled_at) == 2, "both turns must have been cancelled by the drain"
        assert max(cancelled_at) - t0 < 4.0, f"the turn wait started after the slow drain_worker: cancel at +{max(cancelled_at) - t0:.1f}s"
        assert min(cancelled_at) - t0 >= 2.9, "the cancel arrived before drain_worker even returned"
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_slow_stop_claiming_inside_the_allowance_does_not_cut_the_turn_wait():
    """The cap's LOWER side: it allows ``_stop_claiming`` two graces, so a ``_stop_claiming`` that takes 1.8 graces
    still leaves the turn wait its full ``drain_timeout`` after it. Grace 1 s, drain timeout 1 s: the turn wait ends
    at about 2.8 s (the cap is 3 s), so a turn finishing at 2.5 s (timeout + 1.5 graces) is not cut, and a turn that
    never finishes is cancelled at about 2.8 s, not at 1.8 s. With no allowance (``+ 0``) the cap is 1 s: both turns
    would be cancelled the moment ``_stop_claiming`` returned."""
    engine = InMemoryClaimEngine(adapters={KIND: _Adapter()})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    loop = asyncio.get_event_loop()
    started: list[str] = []
    drain_began = asyncio.Event()
    clock: dict[str, float] = {}
    finished: list[str] = []
    cancelled_at: dict[str, float] = {}

    async def handler(lease):
        started.append(lease.entity_id)
        try:
            await drain_began.wait()
            if lease.entity_id == "finishing":
                await asyncio.sleep(clock["t0"] + 2.5 - loop.time())
                finished.append(lease.entity_id)
                return
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_at[lease.entity_id] = loop.time() - clock["t0"]
            raise

    pool = await _pool(engine, scheduler, handler)
    pool._claim_stop_grace_seconds = 1.0
    real_stop = pool._stop_claiming

    async def slow_stop(grace=None):
        await real_stop(grace)
        await asyncio.sleep(clock["t0"] + 1.8 - loop.time())      # 1.8 graces in all

    pool._stop_claiming = slow_stop  # type: ignore[method-assign]
    try:
        for key in ("finishing", "endless"):
            await engine.upsert(KIND, key)
        await _until(lambda: sorted(started) == ["endless", "finishing"], "turns never started")
        clock["t0"] = loop.time()
        drain_began.set()
        await asyncio.wait_for(pool.drain_and_stop(timeout=1.0), timeout=15.0)

        assert finished == ["finishing"], f"a turn inside the allowance was cut: cancelled at {cancelled_at}"
        assert "finishing" not in cancelled_at
        assert 2.7 <= cancelled_at["endless"] < 3.6, (
            f"the turn wait ended at +{cancelled_at['endless']:.2f}s, not one drain timeout after _stop_claiming"
        )
    finally:
        await scheduler.aclose()
