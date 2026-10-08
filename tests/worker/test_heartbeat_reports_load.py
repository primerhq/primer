"""A worker reports how many items it is running with every heartbeat (Lead sweep M2).

``/v1/health`` on an API that runs ``--no-worker`` could not say how loaded the fleet was ("Worker pool: n/a of 6 capacity"): the
durable worker registry holds ``capacity`` but nothing about load, because ``in_flight`` lived only in each worker process's memory.
The pool now writes its in-flight count to the registry on each heartbeat tick (``Scheduler.report_worker_load``), so any API can
sum it. The count is the pool's own ``len(_in_flight)`` at the tick, nothing derived.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.int.claim import ClaimKind
from tests.worker.test_cancel_reconcile import _pool, _until


class _Registry:
    """A scheduler double that records what the pool tells the worker registry."""

    def __init__(self, *, fail_load_reports: int = 0) -> None:
        self.heartbeats: list[str] = []
        self.loads: list[tuple[str, int]] = []
        self._fail = fail_load_reports

    async def heartbeat_worker(self, worker_id: str) -> None:
        self.heartbeats.append(worker_id)

    async def report_worker_load(self, worker_id: str, *, in_flight: int) -> None:
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("the registry is unreachable")
        self.loads.append((worker_id, in_flight))


class _Engine:
    """Confirms every lease it is asked about, so a tick with items in flight completes normally."""

    async def heartbeat(self, worker_id, keys):
        return list(keys)


async def _run_until(pool, predicate, *, within: float = 8.0) -> None:
    task = asyncio.create_task(pool._heartbeat_loop())
    try:
        await asyncio.wait_for(_until(predicate), within)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_each_heartbeat_reports_how_many_items_the_pool_is_running():
    registry = _Registry()
    pool = _pool(scheduler=registry, interval=1)
    pool._engine = _Engine()
    pool._in_flight = {(ClaimKind.SESSION, "a"), (ClaimKind.SESSION, "b")}

    await _run_until(pool, lambda: registry.loads)

    assert registry.loads[0] == ("wrk-test", 2)


@pytest.mark.asyncio
async def test_an_idle_pool_reports_zero_not_nothing():
    registry = _Registry()
    pool = _pool(scheduler=registry, interval=1)

    await _run_until(pool, lambda: registry.loads)

    assert registry.loads[0] == ("wrk-test", 0), "idle is a number the fleet total needs, not an absence"


@pytest.mark.asyncio
async def test_a_registry_that_will_not_take_the_load_does_not_stop_the_heartbeat():
    """Reporting load is advisory: the heartbeat is what keeps the worker alive, and the next tick tries again."""
    registry = _Registry(fail_load_reports=1)
    pool = _pool(scheduler=registry, interval=1)

    await _run_until(pool, lambda: registry.loads, within=10.0)

    assert len(registry.heartbeats) >= 2, "the first tick's failed report must not have ended the loop"
    assert registry.loads[0][1] == 0
