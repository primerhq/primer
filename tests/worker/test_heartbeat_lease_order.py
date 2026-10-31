"""The lease heartbeat must not trail the worker-row writes (CORE-10).

``_heartbeat_loop`` used to write the worker row (``heartbeat_worker``) and
the load (``report_worker_load``) before the engine heartbeat. If those
registry writes are slow (a peer's ALTER TABLE workers taking ACCESS
EXCLUSIVE during a rolling upgrade, a degraded database), one tick can run
past the lease TTL before the lease UPDATE, and a peer claims and re-runs
the same turn. The engine heartbeat - the lease refresh - must go first in
each tick; the two worker-row writes follow, unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.int.claim import ClaimKind
from tests.worker.test_cancel_reconcile import _pool, _until


class _Registry:
    """Scheduler double recording the worker-row writes, optionally failing
    or blocking them."""

    def __init__(
        self, *, fail_heartbeats: bool = False, block_from_call: int | None = None,
    ) -> None:
        self.heartbeats: list[str] = []
        self.loads: list[tuple[str, int]] = []
        self._fail = fail_heartbeats
        self._block_from = block_from_call
        self.release = asyncio.Event()

    async def heartbeat_worker(self, worker_id: str) -> None:
        if self._fail:
            raise RuntimeError("worker registry down")
        self.heartbeats.append(worker_id)
        if self._block_from is not None and len(self.heartbeats) >= self._block_from:
            await self.release.wait()

    async def report_worker_load(self, worker_id: str, *, in_flight: int) -> None:
        self.loads.append((worker_id, in_flight))


class _Engine:
    def __init__(self) -> None:
        self.calls: list[list] = []

    async def heartbeat(self, worker_id: str, scopes) -> list:
        self.calls.append(list(scopes))
        return list(scopes)


async def _run_heartbeat_ticks(pool, predicate, *, within: float = 5.0) -> None:
    task = asyncio.create_task(pool._heartbeat_loop())
    try:
        await asyncio.wait_for(_until(predicate), within)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_each_tick_sends_the_engine_heartbeat_the_worker_row_and_the_load():
    """Characterization (unchanged by the reorder): one tick carries all three."""
    registry = _Registry()
    pool = _pool(scheduler=registry, interval=1)
    engine = _Engine()
    pool._engine = engine
    pool._in_flight = {(ClaimKind.SESSION, "a"), (ClaimKind.SESSION, "b")}

    await _run_heartbeat_ticks(
        pool, lambda: engine.calls and registry.heartbeats and registry.loads,
    )

    assert len(engine.calls[0]) == 2, "both in-flight leases ride the same tick"
    assert registry.heartbeats[0] == "wrk-test"
    assert registry.loads[0] == ("wrk-test", 2)


async def test_a_failed_worker_row_write_does_not_skip_the_engine_heartbeat():
    """Characterization (unchanged by the reorder): the registry is advisory."""
    registry = _Registry(fail_heartbeats=True)
    pool = _pool(scheduler=registry, interval=1)
    engine = _Engine()
    pool._engine = engine
    pool._in_flight = {(ClaimKind.SESSION, "a")}

    await _run_heartbeat_ticks(pool, lambda: engine.calls)

    assert engine.calls


async def test_the_lease_heartbeat_does_not_wait_behind_the_worker_row_writes():
    """Pre-fix red: the engine heartbeat ran after the worker-row writes, so
    a heartbeat_worker that blocks past the interval (a peer's ALTER TABLE
    workers, a degraded database) stalls the lease refresh; the engine
    heartbeat stops being sent every interval."""
    registry = _Registry(block_from_call=2)
    pool = _pool(scheduler=registry, interval=1)
    engine = _Engine()
    pool._engine = engine
    pool._in_flight = {(ClaimKind.SESSION, "a")}

    task = asyncio.create_task(pool._heartbeat_loop())
    try:
        await asyncio.wait_for(_until(lambda: len(engine.calls) >= 2), 5.0)
    finally:
        registry.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert len(engine.calls) >= 2, (
        "the lease heartbeat must still be sent every interval while a "
        "worker-row write is blocked"
    )
