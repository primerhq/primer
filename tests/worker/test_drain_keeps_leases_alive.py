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
    engine.lease_ttl_seconds = 2     # start() applies the config's 5 s floor; a short TTL keeps the test short
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
            await asyncio.sleep(2.6)                              # past the ORIGINAL expiry (ttl 2 s)

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
        drain = asyncio.create_task(pool.drain_and_stop(timeout=20))
        await _until(pool._stopping.is_set, "drain never started")
        gate.set()
        await asyncio.wait_for(drain, timeout=5.0)

        assert pool._keepalive_done.is_set()
        assert all(t.done() for t in pool._tasks) or pool._tasks == [], "a keep-alive loop outlived the drain"
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
