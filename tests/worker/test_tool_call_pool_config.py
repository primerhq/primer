"""Pool and config pins for the 7a tool-call executor (slice S1-C).

None of this runs the executor; it fixes the contracts the executor slices build on, so a later change
that breaks one fails here instead of in a flag-on e2e: the flag and reserve defaults, how the reserve is
derived, which claim loop a config selects, which kinds the unified loop will claim, the priority table,
the heartbeat loop's independence from the scheduler heartbeat, and the reserved loop's real body.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import (
    CLAIM_PRIORITY_FRESH,
    CLAIM_PRIORITY_OPERATOR,
    CLAIM_PRIORITY_RESUME,
    ClaimKind,
    ReleaseOutcome,
)
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool
from primer.worker.turn import _CancelScope


class _SpyAdapter:
    entity_table = "spy"

    def __init__(self, kind: ClaimKind) -> None:
        self.kind = kind

    def eligibility_sql(self) -> str:
        return "true"

    async def on_release(self, conn, entity_id, *, outcome):
        return None


async def _until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


# ---- config pins ----------------------------------------------------------------------------------


def test_the_flag_and_the_reserve_default_to_off_and_unset():
    cfg = WorkerConfig()
    assert cfg.tool_calls_as_claims_enabled is False
    assert cfg.tool_call_reserved_concurrency is None


@pytest.mark.parametrize(
    "concurrency,expected",
    [(1, None), (2, 1), (3, 1), (4, 2), (8, 4), (64, 32)],
)
def test_the_reserve_is_derived_when_the_flag_is_on(concurrency, expected):
    cfg = WorkerConfig(concurrency=concurrency, tool_calls_as_claims_enabled=True)
    assert cfg.tool_call_reserved_concurrency == expected
    if expected is not None:
        assert cfg.concurrency - expected >= 1, "at least one general slot must remain"


def test_an_explicit_reserve_is_kept_and_the_flag_off_derives_nothing():
    assert WorkerConfig(
        concurrency=8, tool_calls_as_claims_enabled=True, tool_call_reserved_concurrency=5,
    ).tool_call_reserved_concurrency == 5
    assert WorkerConfig(concurrency=8).tool_call_reserved_concurrency is None
    assert WorkerConfig(
        concurrency=8, tool_call_reserved_concurrency=3,
    ).tool_call_reserved_concurrency == 3, "the flag off must not rewrite an explicit value"


def test_a_reserve_that_leaves_no_general_slot_is_still_rejected():
    with pytest.raises(ValueError):
        WorkerConfig(concurrency=4, tool_calls_as_claims_enabled=True, tool_call_reserved_concurrency=4)


def test_the_flag_description_no_longer_points_at_a_flock_that_was_dropped():
    desc = WorkerConfig.model_fields["tool_calls_as_claims_enabled"].description or ""
    assert "flock" not in desc.lower()
    assert "docker or kubernetes workspaces" in desc


# ---- priority table ---------------------------------------------------------------------------------


def test_the_priority_table_and_engine_defaults():
    import inspect

    assert (CLAIM_PRIORITY_OPERATOR, CLAIM_PRIORITY_RESUME, CLAIM_PRIORITY_FRESH) == (10, 50, 100)
    from primer.claim.postgres import PostgresClaimEngine
    from primer.int.claim import ClaimEngine

    for engine_cls in (ClaimEngine, InMemoryClaimEngine, PostgresClaimEngine):
        assert inspect.signature(engine_cls.upsert).parameters["priority"].default == CLAIM_PRIORITY_FRESH
        assert (
            inspect.signature(engine_cls.mark_resumable).parameters["priority"].default
            == CLAIM_PRIORITY_RESUME
        )


# ---- the unified loop only claims kinds it can run ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_unified_loop_claims_only_kinds_it_has_a_handler_for():
    """A TOOL_CALL lease left over after the flag was turned off used to be claimed by the unified loop
    ("no handler for kind", the lease then sat claimed until it expired)."""
    engine = InMemoryClaimEngine(adapters={
        ClaimKind.HARNESS: _SpyAdapter(ClaimKind.HARNESS),
        ClaimKind.TOOL_CALL: _SpyAdapter(ClaimKind.TOOL_CALL),
    })
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    seen_kinds: list[list[ClaimKind] | None] = []
    real_claim_due = engine.claim_due

    async def spy_claim_due(worker_id, *, max_count, kinds=None):
        seen_kinds.append(None if kinds is None else list(kinds))
        return await real_claim_due(worker_id, max_count=max_count, kinds=kinds)

    engine.claim_due = spy_claim_due  # type: ignore[method-assign]
    ran: list[str] = []

    async def harness_handler(lease):
        ran.append(lease.entity_id)
        await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5,
            poll_interval_seconds=0.1, drain_timeout_seconds=3,
        ),
        scheduler=scheduler, storage=None,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    try:
        await pool.start()
        pool._dispatch = {ClaimKind.HARNESS: harness_handler}
        await engine.upsert(ClaimKind.TOOL_CALL, "orphan-task")        # nobody here can run it
        await engine.upsert(ClaimKind.HARNESS, "h1")
        await _until(lambda: ran == ["h1"], "the harness lease never ran")
        await asyncio.sleep(0.4)                                       # several polls
        assert engine._leases[(ClaimKind.TOOL_CALL, "orphan-task")].claimed_by is None, (
            "the unified loop claimed a lease it has no handler for"
        )
        assert all(k is not None and ClaimKind.TOOL_CALL not in k for k in seen_kinds)
        assert pool.metrics_snapshot()["primer_worker_claims_total"] == 1
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


# ---- the heartbeat loop: two independent try blocks ----------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_scheduler_heartbeat_does_not_skip_the_engine_heartbeat(caplog):
    """The engine heartbeat is the lease keep-alive and the lost-lease preempt; one try block meant a
    failed worker-row heartbeat skipped it for the whole tick, so a lost lease went unpreempted."""
    class Scheduler:
        async def heartbeat_worker(self, worker_id):
            raise RuntimeError("scheduler down")

        async def report_worker_load(self, worker_id, *, in_flight):
            return None

    class Engine:
        def __init__(self):
            self.calls = 0

        async def heartbeat(self, worker_id, keys):
            self.calls += 1
            return [k for k in keys if k[1] != "lost"]

    engine = Engine()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5),
        scheduler=Scheduler(), storage=None,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    kept, lost = (ClaimKind.HARNESS, "kept"), (ClaimKind.HARNESS, "lost")
    pool._in_flight = {kept, lost}
    pool._active_scopes = {kept: _CancelScope(), lost: _CancelScope()}
    task = asyncio.create_task(pool._heartbeat_loop())
    try:
        with caplog.at_level(logging.ERROR, logger="primer.worker.pool"):
            await _until(lambda: engine.calls >= 1, "the engine heartbeat never ran", timeout=4.0)
            await _until(lambda: pool._active_scopes[lost].cancelled, "the lost lease was not preempted")
        assert not pool._active_scopes[kept].cancelled
        assert any("scheduler heartbeat failed" in r.getMessage() for r in caplog.records)
    finally:
        pool._stopping.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---- the reserved loop, run for real ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reserved_loop_caps_tool_calls_and_never_starves_the_general_slice():
    """Flag on (reserve derived: concurrency 4 -> 2): a burst of TOOL_CALL leases runs at most 2 at a
    time, a HARNESS lease still runs while they hold the reserve, and the rest of the burst follows."""
    engine = InMemoryClaimEngine(adapters={
        ClaimKind.HARNESS: _SpyAdapter(ClaimKind.HARNESS),
        ClaimKind.TOOL_CALL: _SpyAdapter(ClaimKind.TOOL_CALL),
    })
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    config = WorkerConfig(
        concurrency=4, claim_batch_size=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5,
        poll_interval_seconds=0.1, drain_timeout_seconds=3, tool_calls_as_claims_enabled=True,
    )
    assert config.tool_call_reserved_concurrency == 2
    started: list[str] = []
    running_now = 0
    peak = 0
    gate = asyncio.Event()

    async def tool_handler(lease):
        nonlocal running_now, peak
        started.append(lease.entity_id)
        running_now += 1
        peak = max(peak, running_now)
        try:
            await gate.wait()
        finally:
            running_now -= 1
        await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    async def harness_handler(lease):
        started.append(lease.entity_id)
        await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    pool = WorkerPool(
        config=config, scheduler=scheduler, storage=None,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    try:
        assert pool._select_claim_loop() == pool._engine_claim_loop_reserved
        await pool.start()
        pool._dispatch[ClaimKind.TOOL_CALL] = tool_handler
        pool._dispatch[ClaimKind.HARNESS] = harness_handler
        for i in range(5):
            await engine.upsert(ClaimKind.TOOL_CALL, f"t{i}", priority=CLAIM_PRIORITY_RESUME)
        await _until(lambda: len([x for x in started if x.startswith("t")]) == 2, "reserve never filled")
        await engine.upsert(ClaimKind.HARNESS, "h1", priority=CLAIM_PRIORITY_OPERATOR)
        await _until(lambda: "h1" in started, "the general slice was starved by the tool-call burst")
        await asyncio.sleep(0.4)
        assert len([x for x in started if x.startswith("t")]) == 2, "the reserve is an exclusive cap"
        assert peak == 2
        gate.set()
        await _until(lambda: len([x for x in started if x.startswith("t")]) == 5, "the burst never finished")
        assert peak <= 2
    finally:
        gate.set()
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_the_reserved_loops_tool_slice_stays_idle_until_a_handler_is_registered():
    """Flag on, no TOOL_CALL handler (today's build): a claimed lease would be logged "no handler for
    kind" and abandoned to expire, then claimed again for ever. The tool slice claims nothing until a
    handler exists; once one is registered the same lease runs."""
    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: _SpyAdapter(ClaimKind.TOOL_CALL)})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    config = WorkerConfig(
        concurrency=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5,
        poll_interval_seconds=0.1, drain_timeout_seconds=3, tool_calls_as_claims_enabled=True,
    )
    ran: list[str] = []

    async def tool_handler(lease):
        ran.append(lease.entity_id)
        await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    pool = WorkerPool(
        config=config, scheduler=scheduler, storage=None,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    try:
        await pool.start()
        assert ClaimKind.TOOL_CALL not in pool._dispatch
        await engine.upsert(ClaimKind.TOOL_CALL, "t1", priority=CLAIM_PRIORITY_RESUME)
        await asyncio.sleep(0.5)                                       # several polls
        assert engine._leases[(ClaimKind.TOOL_CALL, "t1")].claimed_by is None, "claimed with no handler"
        assert pool.metrics_snapshot()["primer_worker_claims_total"] == 0
        pool._dispatch[ClaimKind.TOOL_CALL] = tool_handler
        await _until(lambda: ran == ["t1"], "the lease never ran once a handler was registered")
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_skipped_tool_slice_is_not_counted_as_an_empty_poll():
    """Flag on, no TOOL_CALL handler, the general slice full: no claim_due is issued at all, so the
    empty-poll counter must not move (the reserve having free capacity is not a poll)."""
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _SpyAdapter(ClaimKind.HARNESS)})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    config = WorkerConfig(
        concurrency=2, heartbeat_interval_seconds=1, lease_ttl_seconds=5,
        poll_interval_seconds=0.1, drain_timeout_seconds=3, tool_calls_as_claims_enabled=True,
    )
    assert config.tool_call_reserved_concurrency == 1
    polls: list[object] = []
    real_claim_due = engine.claim_due

    async def counting_claim_due(worker_id, *, max_count, kinds=None):
        polls.append(kinds)
        return await real_claim_due(worker_id, max_count=max_count, kinds=kinds)

    engine.claim_due = counting_claim_due  # type: ignore[method-assign]
    pool = WorkerPool(
        config=config, scheduler=scheduler, storage=None,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    try:
        await pool.start()
        pool._in_flight.add((ClaimKind.HARNESS, "busy"))               # the one general slot is taken
        await asyncio.sleep(0.6)                                       # several polls of the loop
        assert polls == [], f"claim_due was issued with the general slice full and no tool handler: {polls}"
        assert pool.metrics_snapshot()["primer_worker_claims_empty_total"] == 0
    finally:
        pool._in_flight.discard((ClaimKind.HARNESS, "busy"))
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


class _CountingEvent(asyncio.Event):
    """An Event that counts how many times the loop parks on it."""

    def __init__(self) -> None:
        super().__init__()
        self.waits = 0

    async def wait(self) -> bool:
        self.waits += 1
        return await super().wait()


@pytest.mark.asyncio
async def test_a_failed_claim_due_is_not_an_empty_poll_and_does_not_sleep_twice():
    """The reserved loop's slice already backs off for one poll interval when ``claim_due`` raises; the loop then
    counted that as an empty poll and parked on the wake event for ANOTHER interval. A failure is neither: no
    empty-poll count, no second wait (the unified loop never doubled its sleep on a failure either)."""
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _SpyAdapter(ClaimKind.HARNESS)})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    config = WorkerConfig(
        concurrency=2, heartbeat_interval_seconds=1, lease_ttl_seconds=5,
        poll_interval_seconds=0.1, drain_timeout_seconds=3, tool_calls_as_claims_enabled=True,
    )
    assert config.tool_call_reserved_concurrency == 1
    attempts: list[object] = []

    async def failing_claim_due(worker_id, *, max_count, kinds=None):
        attempts.append(kinds)
        raise RuntimeError("database unavailable")

    engine.claim_due = failing_claim_due  # type: ignore[method-assign]
    pool = WorkerPool(
        config=config, scheduler=scheduler, storage=None,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    wake = _CountingEvent()
    pool._wake = wake
    try:
        await pool.start()
        await _until(lambda: len(attempts) >= 4, "the loop did not keep retrying a failing claim_due")
        snap = pool.metrics_snapshot()
        assert snap["primer_worker_claims_empty_total"] == 0, "a failed claim_due was counted as an empty poll"
        assert wake.waits == 0, f"the loop parked on the wake event {wake.waits} time(s) after a failed claim_due"
    finally:
        engine.claim_due = InMemoryClaimEngine.claim_due.__get__(engine)  # type: ignore[method-assign]
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()
