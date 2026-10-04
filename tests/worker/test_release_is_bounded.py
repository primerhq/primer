"""``WorkerPool._release_lease`` bounds the release it makes.

Once an execution's scope is marked ``lease_returned`` (PR #330) a lost-lease verdict can no longer push a
release that hangs: a stuck connection after the lease was genuinely lost would otherwise keep the handler
alive until the drain timeout. The release is therefore bounded by ``_release_timeout_seconds`` (two lease
TTLs by default): on timeout the release is cancelled, counted and logged, the ``TimeoutError`` propagates
like any failed release, the key leaves ``_in_flight`` (so nothing heartbeats the lease and it expires for a
peer to re-claim), and an UNRELATED execution's release is untouched. A ``TimeoutError`` raised by the engine
itself (a command timeout) is not this bound and is not counted as it.

Every test runs two executions.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

WORKER = "wrk-bounded"


async def _until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


class _World:
    def __init__(self) -> None:
        self.engine = InMemoryClaimEngine(adapters={})
        self.scheduler = InMemoryScheduler()
        self.pool = WorkerPool(
            config=WorkerConfig(concurrency=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5),
            scheduler=self.scheduler, storage=None,  # type: ignore[arg-type]
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=self.engine,
        )
        self.pool._worker_id = WORKER
        self.outcomes: dict[str, str] = {}

        async def releasing(lease) -> None:
            try:
                await self.pool._release_lease(lease, ReleaseOutcome(success=True, drop_lease=True))
                self.outcomes[lease.entity_id] = "released"
            except TimeoutError:
                self.outcomes[lease.entity_id] = "timeout"
                raise

        self.pool._dispatch = {ClaimKind.HARNESS: releasing, ClaimKind.TRIGGER: releasing}

    async def start(self) -> list:
        await self.scheduler.initialize()
        for kind, eid in ((ClaimKind.HARNESS, "stuck"), (ClaimKind.TRIGGER, "fine")):
            await self.engine.upsert(kind, eid)
        leases = await self.engine.claim_due(WORKER, max_count=10)
        assert len(leases) == 2
        return leases

    async def close(self) -> None:
        await self.scheduler.aclose()


@pytest.mark.asyncio
async def test_a_release_that_hangs_is_abandoned_within_the_bound_and_the_other_execution_is_untouched():
    w = _World()
    leases = await w.start()
    real_release = w.engine.release
    hung = asyncio.Event()

    async def release(lease, *, outcome):
        if lease.entity_id == "stuck":
            hung.set()
            await asyncio.Event().wait()   # never returns
        await real_release(lease, outcome=outcome)

    w.engine.release = release  # type: ignore[method-assign]
    w.pool._release_timeout_seconds = 0.3
    try:
        w.pool._reserve_and_dispatch(leases)
        await asyncio.wait_for(hung.wait(), timeout=5.0)
        await _until(lambda: not w.pool._in_flight, "an execution is still in flight: the hung release was not bounded")
        assert w.outcomes == {"stuck": "timeout", "fine": "released"}
        assert w.pool._release_timeouts_total == 1
        assert w.pool.metrics_snapshot()["primer_worker_release_timeouts_total"] == 1
        # the unrelated execution's lease really went back; the stuck one's lease is left to expire
        assert not await w.engine.has_lease(ClaimKind.TRIGGER, "fine")
        assert await w.engine.has_lease(ClaimKind.HARNESS, "stuck")
    finally:
        for task in list(w.pool._turn_tasks):
            task.cancel()
        await asyncio.gather(*w.pool._turn_tasks, return_exceptions=True)
        await w.close()


@pytest.mark.asyncio
async def test_a_timeout_raised_by_the_engine_itself_is_not_counted_as_the_bound():
    w = _World()
    leases = await w.start()
    real_release = w.engine.release

    async def release(lease, *, outcome):
        if lease.entity_id == "stuck":
            raise TimeoutError("command timeout from the driver")
        await real_release(lease, outcome=outcome)

    w.engine.release = release  # type: ignore[method-assign]
    w.pool._release_timeout_seconds = 30.0
    try:
        w.pool._reserve_and_dispatch(leases)
        await _until(lambda: not w.pool._in_flight, "executions did not finish")
        assert w.outcomes == {"stuck": "timeout", "fine": "released"}   # the handler saw the TimeoutError either way
        assert w.pool._release_timeouts_total == 0
    finally:
        for task in list(w.pool._turn_tasks):
            task.cancel()
        await asyncio.gather(*w.pool._turn_tasks, return_exceptions=True)
        await w.close()
