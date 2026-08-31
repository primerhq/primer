"""S7 section 4: per-lane worker task counters, bounded by lease kind."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool


@pytest.fixture(autouse=True)
def _reset_metrics():
    import primer.observability.metrics as m
    m.reset_for_test()
    yield
    m.reset_for_test()


@pytest.fixture
async def pool():
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    p = WorkerPool(
        config=WorkerConfig(concurrency=1, worker_label="lane-a"),
        scheduler=scheduler,
        storage=None,                  # type: ignore[arg-type]
        workspace_registry=None,       # type: ignore[arg-type]
        provider_registry=None,        # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )
    yield p
    await scheduler.aclose()


def _lease(kind: ClaimKind) -> Lease:
    now = datetime.now(timezone.utc)
    return Lease(
        kind=kind,
        entity_id="e-1",
        claimed_by="wrk-x",
        claimed_at=now,
        expires_at=now + timedelta(seconds=30),
        attempt_count=1,
        last_error=None,
    )


async def test_success_counts_ok(pool):
    import primer.observability.metrics as m

    async def _handler(_lease):
        return None

    await pool._run_engine(_lease(ClaimKind.SESSION), _handler)
    val = m.worker_tasks_total.labels("lane-a", "session", "ok")._value.get()
    assert val == 1.0
    count = m.worker_task_duration_seconds.labels(
        "lane-a", "session", "ok",
    )._sum.get()
    assert count >= 0.0


async def test_handler_exception_counts_error(pool):
    import primer.observability.metrics as m

    async def _handler(_lease):
        raise RuntimeError("boom")

    await pool._run_engine(_lease(ClaimKind.HARNESS), _handler)
    val = m.worker_tasks_total.labels("lane-a", "harness", "error")._value.get()
    assert val == 1.0


async def test_cancellation_counts_cancelled(pool):
    import primer.observability.metrics as m

    async def _handler(_lease):
        raise asyncio.CancelledError()

    await pool._run_engine(_lease(ClaimKind.TRIGGER), _handler)
    val = m.worker_tasks_total.labels("lane-a", "trigger", "cancelled")._value.get()
    assert val == 1.0


async def test_kind_label_is_bounded_to_claim_kinds(pool):
    import primer.observability.metrics as m

    async def _handler(_lease):
        return None

    # Iterate the enum, never a hand-written member list: S6 P5 deletes
    # the CHAT lane (S1 P7's C4 carve-out) before S7 runs, and a literal
    # {"session", "chat", "harness", "trigger"} would go red the moment it
    # does. The claim is "kind is bounded BY ClaimKind", which is the
    # cardinality property the allowlist guard cares about.
    for kind in ClaimKind:
        await pool._run_engine(_lease(kind), _handler)
    seen = {
        s.labels["kind"]
        for metric in m.worker_tasks_total.collect()
        for s in metric.samples
        if s.name == "worker_tasks_total"
    }
    assert seen == {k.value for k in ClaimKind}
    assert "session" in seen
