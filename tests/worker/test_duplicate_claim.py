"""A same-worker duplicate claim of an in-flight lease dispatches nothing.

``claim_due`` re-claims any lease whose ``expires_at`` has passed, INCLUDING this worker's
own claim. When a worker's heartbeat stalls past the lease TTL while a turn is still running,
its own claim loop therefore claims the SAME ``(kind, id)`` again. ``worker_id`` is minted per
pool start, so the duplicate can only ever land inside the pool that is still running the
first execution (the handler releases before ``_run_engine``'s ``finally`` discards the key).

The pool must leave that duplicate completely alone: no second executor, no replaced cancel
scope, no release, no requeue. The re-claim already re-stamped the very row the in-flight
execution's heartbeat keeps alive, so the first execution carries on, is confirmed by its next
heartbeat, and its own release (which carries the OLD ``claimed_at``) reaches ``on_release``.

Every test uses two keys: one duplicate-claimed, one untouched and asserted unaffected.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease as ClaimLease
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool
from primer.worker.turn import _CancelScope
from tests.worker._duplicate_claim_scenario import run_reclaim_scenario


class _SpyAdapter:
    """Records every ``on_release`` so a skipped release is visible."""

    entity_table = "spy"

    def __init__(self, kind: ClaimKind) -> None:
        self.kind = kind
        self.released: list[str] = []

    def eligibility_sql(self) -> str:
        return "true"

    async def on_release(self, conn, entity_id, *, outcome):
        self.released.append(entity_id)


def _lease(kind: ClaimKind, entity_id: str, claimed_by: str) -> ClaimLease:
    now = datetime.now(UTC)
    return ClaimLease(
        kind=kind, entity_id=entity_id, claimed_by=claimed_by, claimed_at=now,
        expires_at=now + timedelta(seconds=30), attempt_count=0, last_error=None,
    )


def _config() -> WorkerConfig:
    # heartbeat_interval_seconds is an int >= 1 and lease_ttl >= 2 * heartbeat, so these are
    # the tightest values the model allows. The test forces the expiry by hand instead of
    # waiting out the TTL.
    return WorkerConfig(
        concurrency=4, claim_batch_size=4, heartbeat_interval_seconds=1,
        lease_ttl_seconds=5, poll_interval_seconds=0.1, drain_timeout_seconds=5,
    )


@pytest.mark.asyncio
async def test_reserve_and_dispatch_skips_a_lease_already_in_flight(caplog):
    """Unit level: the duplicate neither dispatches, nor replaces the scope, nor releases."""
    adapter = _SpyAdapter(ClaimKind.SESSION)
    engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: adapter})
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
        dispatched: list[str] = []
        gate = asyncio.Event()

        async def _handler(lease):
            dispatched.append(lease.entity_id)
            await gate.wait()

        pool._dispatch = {ClaimKind.SESSION: _handler}

        in_flight_key = (ClaimKind.SESSION, "s1")
        original_scope = _CancelScope()
        pool._in_flight.add(in_flight_key)
        pool._active_scopes[in_flight_key] = original_scope

        with caplog.at_level(logging.WARNING, logger="primer.worker.pool"):
            pool._reserve_and_dispatch([
                _lease(ClaimKind.SESSION, "s1", "wrk-test"),   # the duplicate
                _lease(ClaimKind.SESSION, "s2", "wrk-test"),   # a genuinely new claim
            ])
            await asyncio.sleep(0)

        assert dispatched == ["s2"], "the duplicate of an in-flight key must not be dispatched"
        assert pool._active_scopes[in_flight_key] is original_scope, (
            "the in-flight execution's cancel scope was replaced"
        )
        assert in_flight_key in pool._in_flight
        assert adapter.released == [], "the duplicate lease must not be released"
        assert pool.metrics_snapshot()["primer_worker_duplicate_claims_total"] == 1
        assert any(
            "s1" in r.getMessage() and "duplicate" in r.getMessage().lower()
            for r in caplog.records
        ), "the skipped duplicate must be logged"

        gate.set()
        for task in list(pool._turn_tasks):
            await asyncio.wait_for(task, timeout=1.0)
        # s2 (the new claim) is tracked and cleaned up normally.
        assert (ClaimKind.SESSION, "s2") not in pool._in_flight
        assert in_flight_key in pool._in_flight, "the skip must not discard the live key"
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ClaimKind.SESSION, ClaimKind.HARNESS])
async def test_same_worker_reclaim_of_in_flight_key_neither_preempts_nor_redispatches(
    kind, fake_storage_provider, fake_provider_registry,
):
    """Pool level, real claim and heartbeat loops, in-memory engine.

    The scenario (tests/worker/_duplicate_claim_scenario.py) is shared with the Postgres
    variant in tests/claim/test_postgres_engine.py.
    """
    adapter = _SpyAdapter(kind)
    engine = InMemoryClaimEngine(adapters={kind: adapter})
    scheduler = InMemoryScheduler(storage_provider=fake_storage_provider)
    pool = WorkerPool(
        config=_config(), scheduler=scheduler, storage=fake_storage_provider,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=fake_provider_registry, engine=engine,
    )

    async def _force_expired(k: ClaimKind, entity_id: str) -> None:
        engine._leases[(k, entity_id)].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    async def _lease_state(k: ClaimKind, entity_id: str):
        row = engine._leases.get((k, entity_id))
        return None if row is None else (row.claimed_by, row.claimed_at)

    await scheduler.initialize()
    try:
        await run_reclaim_scenario(
            kind=kind, pool=pool, engine=engine, released=adapter.released,
            ids=("dup", "other"), force_expired=_force_expired, lease_state=_lease_state,
        )
    finally:
        await scheduler.aclose()
