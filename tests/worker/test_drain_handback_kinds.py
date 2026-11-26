"""H-2 deferred: a lease cut by a drain timeout or the staleness self-cancel is handed back
claimable for EVERY kind, not just sessions.

The pool records the cut keys in ``_hand_back_keys`` (the drain timeout, ``_cancel_stale_scopes``),
and the session path already releases them with the hand-back outcome (``entity_noop``: the lease
stays claimable, the entity untouched, the next claim re-runs the work as a crash leaves it). The
other kinds released a cut with the default delete-the-lease failure: a harness operation was
finalised as ERROR and never re-ran, a trigger fire advanced its schedule and dropped the lease,
a memory job's lease was deleted. Now every non-session handler takes the hand-back outcome for a
cut it made itself, and the lease watchdog's per-check call cannot kill the watchdog.

Driven through the real handler, the real pool ``_release_lease`` and the real
``InMemoryClaimEngine`` (+ the ``HarnessClaimAdapter`` for the harness leg), with the
kind's work seam monkeypatched to raise the cut (``CancelledError``).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.harness import dispatch as harness_dispatch
from primer.int.claim import ClaimKind
from primer.model.harness import Harness, HarnessOperation
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.storage.factory import StorageProviderFactory
from primer.worker.engine_handlers import run_engine_harness, run_engine_memory
from primer.worker.pool import WorkerPool
from tests._support.harness_ops import enqueued
from tests.claim._platform import platform_adapters


@pytest_asyncio.fixture
async def sp(tmp_path):
    p = StorageProviderFactory.create(StorageProviderConfig(
        provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "handback.sqlite")))
    await p.initialize()
    await p.get_content_store().ensure_schema()
    yield p
    await p.aclose()


def _harness(hid: str = "cut-id") -> Harness:
    return Harness(
        id=hid, slug="acme", name="Acme", git_url="https://github.com/x/y",
        created_at=datetime.now(timezone.utc),
    )


class _Bus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, key, payload):
        self.events.append(key)


async def _pool(sp, engine, *, bus=None) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, heartbeat_interval_seconds=1, lease_ttl_seconds=5),
        scheduler=InMemoryScheduler(), storage=sp,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
        engine=engine, event_bus=bus,  # type: ignore[arg-type]
    )
    await pool._scheduler.initialize()
    pool._worker_id = "w1"
    return pool


async def _cut(*_a, **_kw):
    raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_a_drain_cut_harness_claim_hands_its_lease_back_claimable(sp, monkeypatch):
    """The cut's harness operation is still pending: the release must leave the row untouched
    and the lease claimable, so the next claim re-runs the operation (as a crash leaves it)."""
    harness_storage = sp.get_storage(Harness)
    h = enqueued(_harness(), HarnessOperation.SYNC)
    await harness_storage.create(h)
    engine = InMemoryClaimEngine(adapters={
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=harness_storage),
    })
    pool = await _pool(sp, engine)
    await engine.upsert(ClaimKind.HARNESS, h.id)
    [lease] = await engine.claim_due("w1", max_count=1, kinds=[ClaimKind.HARNESS])
    pool._hand_back_keys.add((ClaimKind.HARNESS, lease.entity_id))  # the drain/self-cancel recorded the cut

    monkeypatch.setattr(harness_dispatch, "run_one_harness_operation", _cut)
    with pytest.raises(asyncio.CancelledError):
        await run_engine_harness(pool, lease)

    fresh = await harness_storage.get(lease.entity_id)
    assert fresh.pending_operation is not None, "the cut operation must survive, re-runnable by the next claim"
    assert await engine.has_lease(ClaimKind.HARNESS, lease.entity_id), (
        "the cut lease must stay claimable (the hand-back), not be dropped by the failure release",
    )


@pytest.mark.asyncio
async def test_a_cut_harness_claim_does_not_publish_done(sp, monkeypatch):
    """The cut's operation is still pending for the next claim, so the handler must not publish
    harness:{id}:done: a subscriber that reacts to done by enqueuing the next operation would
    land on top of the still-pending one (review 2026-11-23, H-1)."""
    harness_storage = sp.get_storage(Harness)
    h = enqueued(_harness("cut-done"), HarnessOperation.SYNC)
    await harness_storage.create(h)
    engine = InMemoryClaimEngine(adapters={
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=harness_storage),
    })
    bus = _Bus()
    pool = await _pool(sp, engine, bus=bus)
    await engine.upsert(ClaimKind.HARNESS, h.id)
    [lease] = await engine.claim_due("w1", max_count=1, kinds=[ClaimKind.HARNESS])
    pool._hand_back_keys.add((ClaimKind.HARNESS, lease.entity_id))  # the drain/self-cancel recorded the cut

    monkeypatch.setattr(harness_dispatch, "run_one_harness_operation", _cut)
    with pytest.raises(asyncio.CancelledError):
        await run_engine_harness(pool, lease)

    assert bus.events == [], f"the still-pending operation must not announce done: {bus.events}"


@pytest.mark.asyncio
async def test_a_drain_cut_memory_claim_hands_its_lease_back(sp, monkeypatch):
    """The cut's memory job never ran: its lease stays claimable for the next claim instead of
    being dropped by the default failure release (the job's own trigger would re-enqueue it much
    later, not on the next claim)."""
    from primer.memory import jobs as memory_jobs

    # The cut's job never ran and no MemoryJob row exists; the lease is a
    # hand-back token, so the kind stands in with the platform contract.
    engine = InMemoryClaimEngine(adapters=platform_adapters(ClaimKind.MEMORY))
    pool = await _pool(sp, engine)
    await engine.upsert(ClaimKind.MEMORY, "m1")
    [lease] = await engine.claim_due("w1", max_count=1, kinds=[ClaimKind.MEMORY])
    pool._hand_back_keys.add((ClaimKind.MEMORY, lease.entity_id))

    monkeypatch.setattr(memory_jobs, "run_memory_job", _cut)
    with pytest.raises(asyncio.CancelledError):
        await run_engine_memory(pool, lease)

    assert await engine.has_lease(ClaimKind.MEMORY, lease.entity_id), (
        "the cut lease must stay claimable (the hand-back), not be dropped by the failure release",
    )


@pytest.mark.asyncio
async def test_the_lease_watchdog_survives_a_check_that_raises(sp, monkeypatch):
    """A per-check failure (a state the check cannot parse) must not kill the watchdog: the
    next check still runs and a genuinely stale lease is still cut."""
    pool = await _pool(sp, InMemoryClaimEngine(adapters={}))
    checks: list[int] = []

    def _flaky_check():
        checks.append(1)
        if len(checks) == 1:
            raise RuntimeError("a state the check cannot parse")

    monkeypatch.setattr(pool, "_cancel_stale_scopes", _flaky_check)
    task = asyncio.create_task(pool._lease_watchdog())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.5
        while len(checks) < 2 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert len(checks) >= 2, "the watchdog died on its first check that raised"
    finally:
        pool._keepalive_done.set()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        await pool._scheduler.aclose()
