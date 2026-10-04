"""A draining pool starts nothing new and hands back what it had just claimed.

``drain_and_stop`` sets ``_stopping`` and then waits for in-flight turns. A claim that is ALREADY
inside ``claim_due`` when that happens returns its leases afterwards, and ``_reserve_and_dispatch`` used to
dispatch them without looking at ``_stopping``: the worker started turns it was already committed to killing
at the drain timeout, so every rollout that coincided with a claim produced a failed or half-run turn. (The
claim loop itself already exits at its next check once ``_stopping`` is set; only the iteration in flight
was the hole, and cancelling it mid-``claim_due`` instead would strand rows claimed here with nothing
running them until their TTL.)

Now: a lease claimed across the boundary is returned untouched
(``entity_noop``: no ``on_release``, the entity row is not read or written, so a resumable session's park
survives) and requeued for a peer, and drain waits for those hand-backs.

Every test uses two keys.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
import logging

from primer.int.claim import ClaimKind, Lease as ClaimLease, ReleaseOutcome
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

KIND = ClaimKind.HARNESS


class _SpyAdapter:
    entity_table = "spy"
    kind = KIND

    def __init__(self) -> None:
        self.released: list[str] = []

    def eligibility_sql(self) -> str:
        return "true"

    async def on_release(self, conn, entity_id, *, outcome):
        self.released.append(entity_id)


def _config() -> WorkerConfig:
    return WorkerConfig(
        concurrency=8, claim_batch_size=4, heartbeat_interval_seconds=1,
        lease_ttl_seconds=5, poll_interval_seconds=0.1, drain_timeout_seconds=5,
    )


def _lease(entity_id: str, worker: str = "wrk-test") -> ClaimLease:
    now = datetime.now(UTC)
    return ClaimLease(
        kind=KIND, entity_id=entity_id, claimed_by=worker, claimed_at=now,
        expires_at=now + timedelta(seconds=30), attempt_count=0, last_error=None,
    )


async def _until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


async def _pool(engine: InMemoryClaimEngine, scheduler: InMemoryScheduler, handler) -> WorkerPool:
    pool = WorkerPool(
        config=_config(), scheduler=scheduler,
        storage=None,  # type: ignore[arg-type]
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=engine,
    )
    await pool.start()
    pool._dispatch[KIND] = handler
    return pool


@pytest.mark.asyncio
async def test_reserve_and_dispatch_returns_leases_untouched_once_stopping():
    adapter = _SpyAdapter()
    engine = InMemoryClaimEngine(adapters={KIND: adapter})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    try:
        dispatched: list[str] = []

        async def handler(lease):
            dispatched.append(lease.entity_id)

        pool = WorkerPool(
            config=_config(), scheduler=scheduler,
            storage=None,  # type: ignore[arg-type]
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=engine,
        )
        pool._worker_id = "wrk-test"
        pool._dispatch = {KIND: handler}
        for key in ("a", "b", "dup"):
            await engine.upsert(KIND, key)
        claimed = await engine.claim_due("wrk-test", max_count=8)
        assert {x.entity_id for x in claimed} == {"a", "b", "dup"}
        pool._in_flight.add((KIND, "dup"))                  # an execution this pool already has
        pool._stopping.set()

        pool._reserve_and_dispatch(claimed)
        await asyncio.sleep(0.05)
        pending = list(getattr(pool, "_unstarted_releases", ()))
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=2.0)

        assert dispatched == [], "a stopping pool dispatched a just-claimed lease"
        assert (KIND, "a") not in pool._in_flight and (KIND, "b") not in pool._in_flight
        # handed back untouched: requeued for a peer, and no on_release ran (the entity is not read)
        for key in ("a", "b"):
            assert engine._leases[(KIND, key)].claimed_by is None
        assert adapter.released == []
        # the same-worker duplicate of an in-flight key is NOT touched (#312): still claimed by us
        assert engine._leases[(KIND, "dup")].claimed_by == "wrk-test"
        snap = pool.metrics_snapshot()
        assert snap["primer_worker_claims_returned_on_drain_total"] == 2
        assert snap["primer_worker_duplicate_claims_total"] == 1
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_claim_in_flight_when_drain_starts_is_returned_and_a_peer_runs_it():
    """The race itself: claim_due has claimed rows and is about to return them when shutdown begins."""
    adapter = _SpyAdapter()
    engine = InMemoryClaimEngine(adapters={KIND: adapter})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    ran_on: dict[str, list[str]] = {"a": [], "b": []}
    pools: list[WorkerPool] = []
    try:
        def make_handler(label: str):
            async def handler(lease):
                ran_on[label].append(lease.entity_id)
                await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))
            return handler

        pool_a = await _pool(engine, scheduler, make_handler("a"))
        pools.append(pool_a)

        entered, gate = asyncio.Event(), asyncio.Event()
        real_claim_due = engine.claim_due

        async def blocking_claim_due(worker_id, **kw):
            leases = await real_claim_due(worker_id, **kw)      # the rows ARE claimed ...
            if leases and worker_id == pool_a.worker_id and not entered.is_set():
                entered.set()
                await gate.wait()                                # ... but not yet returned
            return leases

        engine.claim_due = blocking_claim_due  # type: ignore[method-assign]
        await engine.upsert(KIND, "k1")
        await engine.upsert(KIND, "k2")
        await asyncio.wait_for(entered.wait(), timeout=3.0)

        drain = asyncio.create_task(pool_a.drain_and_stop(timeout=5))
        await _until(pool_a._stopping.is_set, "drain never started")
        gate.set()                                               # claim_due now returns its leases
        await asyncio.wait_for(drain, timeout=10.0)

        assert ran_on["a"] == [], "the draining pool started a turn it had claimed across shutdown"
        for key in ("k1", "k2"):
            assert engine._leases[(KIND, key)].claimed_by is None, "the lease was not handed back"
        assert adapter.released == [], "a hand-back ran on_release"

        pool_b = await _pool(engine, scheduler, make_handler("b"))
        pools.append(pool_b)
        await _until(lambda: sorted(ran_on["b"]) == ["k1", "k2"], "a peer never ran the returned leases")
        assert ran_on["a"] == []
    finally:
        for p in pools:
            await p.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_draining_pool_claims_nothing_while_it_waits_for_running_turns():
    """A pin, not a regression test: the claim loop exits on ``_stopping``, so work that arrives
    mid-drain must stay claimable for a peer for the whole wait."""
    adapter = _SpyAdapter()
    engine = InMemoryClaimEngine(adapters={KIND: adapter})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    started: list[str] = []
    gate = asyncio.Event()

    async def handler(lease):
        started.append(lease.entity_id)
        await gate.wait()

    pool = await _pool(engine, scheduler, handler)
    try:
        await engine.upsert(KIND, "running")
        await _until(lambda: started == ["running"], "the first turn never started")

        drain = asyncio.create_task(pool.drain_and_stop(timeout=5))
        await _until(pool._stopping.is_set, "drain never started")
        await engine.upsert(KIND, "late")                        # work arrives mid-drain
        await asyncio.sleep(0.6)                                 # many poll intervals (0.1 s)

        assert started == ["running"], "the draining pool claimed and started new work"
        assert engine._leases[(KIND, "late")].claimed_by is None, "the late lease was claimed"
        snap = pool.metrics_snapshot()
        assert snap["primer_worker_claims_returned_on_drain_total"] == 0, "it claimed, then handed back"
        assert snap["primer_worker_claims_total"] == 1, "only the first turn was ever claimed"
        assert not drain.done(), "drain should still be waiting on the running turn"
        gate.set()
        await asyncio.wait_for(drain, timeout=10.0)
        assert started == ["running"]
    finally:
        gate.set()
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_hand_back_keeps_a_resumable_sessions_park_with_the_real_session_adapter(
    fake_storage_provider,
):
    """The reason for entity_noop: the session adapter's non-park on_release would clear the park and
    bump turn_no for a turn that never ran. Use the REAL adapter, not a spy."""
    from primer.claim.adapters.sessions import SessionClaimAdapter
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w", binding=AgentSessionBinding(agent_id="a"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC), turn_no=4,
        parked_status="resumable", parked_state={"resume_event_key": "k"},
        parked_event_key="k", parked_at=datetime.now(UTC),
    ))
    engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=sessions)})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    try:
        pool = WorkerPool(
            config=_config(), scheduler=scheduler, storage=fake_storage_provider,
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=engine,
        )
        pool._worker_id = "wrk-test"
        await engine.upsert(ClaimKind.SESSION, "s1")
        [lease] = await engine.claim_due("wrk-test", max_count=4)
        pool._stopping.set()
        pool._reserve_and_dispatch([lease])
        await asyncio.wait_for(asyncio.gather(*pool._unstarted_releases), timeout=2.0)

        row = await sessions.get("s1")
        assert row is not None
        assert (row.parked_status, row.turn_no, row.parked_state) == ("resumable", 4, {"resume_event_key": "k"})
        assert engine._leases[(ClaimKind.SESSION, "s1")].claimed_by is None
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_failed_hand_back_is_logged_and_does_not_block_drain(caplog):
    adapter = _SpyAdapter()
    engine = InMemoryClaimEngine(adapters={KIND: adapter})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    try:
        pool = WorkerPool(
            config=_config(), scheduler=scheduler,
            storage=None,  # type: ignore[arg-type]
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=engine,
        )
        pool._worker_id = "wrk-test"

        async def boom(lease, *, outcome):
            raise RuntimeError("database went away")

        engine.release = boom  # type: ignore[method-assign]
        pool._stopping.set()
        with caplog.at_level(logging.ERROR, logger="primer.worker.pool"):
            pool._reserve_and_dispatch([_lease("a"), _lease("b")])
            await asyncio.wait_for(pool._stop_claiming(grace=1.0), timeout=3.0)
        assert sum("returning unstarted lease" in r.getMessage() for r in caplog.records) == 2
        assert not pool._unstarted_releases
        snap = pool.metrics_snapshot()
        assert snap["primer_worker_claims_returned_on_drain_total"] == 0, "a failed hand-back was counted as returned"
        assert snap["primer_worker_claim_returns_failed_on_drain_total"] == 2
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_stuck_claim_is_cancelled_after_the_grace_and_drain_completes():
    adapter = _SpyAdapter()
    engine = InMemoryClaimEngine(adapters={KIND: adapter})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    stuck = asyncio.Event()
    try:
        async def never(lease):
            await asyncio.sleep(0)

        pool = await _pool(engine, scheduler, never)
        pool._claim_stop_grace_seconds = 0.3

        async def stuck_claim_due(worker_id, **kw):
            stuck.set()
            await asyncio.Event().wait()                         # a database that never answers

        engine.claim_due = stuck_claim_due  # type: ignore[method-assign]
        await asyncio.wait_for(stuck.wait(), timeout=3.0)
        t0 = time.monotonic()
        await asyncio.wait_for(pool.drain_and_stop(timeout=2), timeout=8.0)
        assert time.monotonic() - t0 < 5.0
        assert pool._engine_claim_task is None
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_claim_loop_idle_in_a_long_poll_notices_shutdown_promptly():
    """drain sets _stopping and then _wake; the loop's own wake.clear() could eat that wake and leave it
    waiting out a long poll interval. It must re-check _stopping after the clear."""
    adapter = _SpyAdapter()
    engine = InMemoryClaimEngine(adapters={KIND: adapter})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    entered, gate = asyncio.Event(), asyncio.Event()
    try:
        async def idle(lease):
            await asyncio.sleep(0)

        pool = WorkerPool(
            config=WorkerConfig(
                concurrency=8, claim_batch_size=4, heartbeat_interval_seconds=1,
                lease_ttl_seconds=5, poll_interval_seconds=30.0, drain_timeout_seconds=5,
            ),
            scheduler=scheduler,
            storage=None,  # type: ignore[arg-type]
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=engine,
        )
        await pool.start()
        pool._dispatch[KIND] = idle

        async def slow_empty_claim_due(worker_id, **kw):
            entered.set()
            await gate.wait()
            return []                                             # nothing claimed; the loop will idle

        engine.claim_due = slow_empty_claim_due  # type: ignore[method-assign]
        pool._wake.set()
        await asyncio.wait_for(entered.wait(), timeout=3.0)       # the loop is inside claim_due
        pool._stopping.set()
        pool._wake.set()                                          # exactly what drain_and_stop does
        gate.set()                                                # claim_due returns empty -> clear()+wait
        # asyncio.wait, not wait_for: the claim loop swallows CancelledError, so a wait_for that times out
        # cancels it and still "succeeds", hiding exactly the hang this test exists to catch.
        _, pending = await asyncio.wait({pool._engine_claim_task}, timeout=3.0)
        assert not pending, "the claim loop sat out its long poll interval instead of noticing shutdown"
    finally:
        gate.set()
        await scheduler.aclose()
