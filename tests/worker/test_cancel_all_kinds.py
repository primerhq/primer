"""_run_engine registers a cancel scope for every claim kind so the
heartbeat loop can preempt a running turn on lease loss (not just SESSION)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease as ClaimLease
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool


def _make_lease(kind: ClaimKind, entity_id: str, claimed_by: str) -> ClaimLease:
    now = datetime.now(timezone.utc)
    return ClaimLease(
        kind=kind,
        entity_id=entity_id,
        claimed_by=claimed_by,
        claimed_at=now,
        expires_at=now + timedelta(seconds=30),
        attempt_count=1,
        last_error=None,
    )


@pytest.mark.asyncio
async def test_run_engine_registers_and_cancels_scope_for_chat():
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    try:
        pool = WorkerPool(
            config=WorkerConfig(concurrency=2),
            scheduler=scheduler,
            storage=None,  # type: ignore[arg-type]
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=InMemoryClaimEngine(adapters={}),
        )
        pool._worker_id = "wrk-test"

        started = asyncio.Event()
        cancelled = {"hit": False}

        async def _handler(lease):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled["hit"] = True
                raise

        lease = _make_lease(ClaimKind.CHAT, "c1", "wrk-test")
        pool._in_flight.add((ClaimKind.CHAT, "c1"))
        task = asyncio.create_task(pool._run_engine(lease, _handler))
        await asyncio.wait_for(started.wait(), timeout=1.0)

        scope = pool._active_scopes.get((ClaimKind.CHAT, "c1"))
        assert scope is not None
        scope.cancel("preempted")
        await asyncio.wait_for(task, timeout=1.0)

        assert cancelled["hit"] is True
        assert (ClaimKind.CHAT, "c1") not in pool._active_scopes  # cleaned up
        assert (ClaimKind.CHAT, "c1") not in pool._in_flight
    finally:
        await scheduler.aclose()
