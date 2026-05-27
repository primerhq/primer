"""Tests for Scheduler.claim_harnesses / heartbeat_harness / release_harness."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from matrix.model.harness import Harness, HarnessOperation, HarnessStatus
from matrix.scheduler.in_memory import InMemoryScheduler


def _make_harness(
    *, id="h1", slug="sc", pending=None, claimed_by=None,
    last_heartbeat_at=None, status=HarnessStatus.DRAFT,
):
    return Harness(
        id=id, slug=slug, name="x",
        git_url="https://x/y",
        pending_operation=pending,
        claimed_by=claimed_by,
        last_heartbeat_at=last_heartbeat_at,
        status=status,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_claim_returns_empty_when_nothing_pending(fake_storage_provider):
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    await fake_storage_provider.get_storage(Harness).create(_make_harness())
    out = await sched.claim_harnesses("w1", max_count=10)
    assert out == []


@pytest.mark.asyncio
async def test_claim_picks_pending_unclaimed(fake_storage_provider):
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    h = _make_harness(pending=HarnessOperation.FETCH)
    await fake_storage_provider.get_storage(Harness).create(h)
    leases = await sched.claim_harnesses("w1", max_count=10)
    assert len(leases) == 1
    assert leases[0].harness_id == "h1"
    assert leases[0].worker_id == "w1"
    assert leases[0].operation == HarnessOperation.FETCH
    row = await fake_storage_provider.get_storage(Harness).get("h1")
    assert row.claimed_by == "w1"


@pytest.mark.asyncio
async def test_claim_skips_freshly_claimed(fake_storage_provider):
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    now = datetime.now(timezone.utc)
    h = _make_harness(
        pending=HarnessOperation.SYNC,
        claimed_by="other",
        last_heartbeat_at=now,
    )
    await fake_storage_provider.get_storage(Harness).create(h)
    leases = await sched.claim_harnesses("w1", max_count=10)
    assert leases == []


@pytest.mark.asyncio
async def test_claim_reclaims_stale(fake_storage_provider):
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    stale = datetime.now(timezone.utc) - timedelta(seconds=120)
    h = _make_harness(
        pending=HarnessOperation.SYNC,
        claimed_by="dead",
        last_heartbeat_at=stale,
    )
    await fake_storage_provider.get_storage(Harness).create(h)
    leases = await sched.claim_harnesses("w1", max_count=10)
    assert len(leases) == 1
    assert leases[0].worker_id == "w1"


@pytest.mark.asyncio
async def test_heartbeat_returns_true_for_owner(fake_storage_provider):
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    h = _make_harness(
        pending=HarnessOperation.FETCH,
        claimed_by="w1",
        last_heartbeat_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(Harness).create(h)
    ok = await sched.heartbeat_harness("h1", "w1")
    assert ok is True


@pytest.mark.asyncio
async def test_heartbeat_returns_false_for_non_owner(fake_storage_provider):
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    h = _make_harness(
        pending=HarnessOperation.FETCH,
        claimed_by="other",
        last_heartbeat_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(Harness).create(h)
    ok = await sched.heartbeat_harness("h1", "w1")
    assert ok is False


@pytest.mark.asyncio
async def test_release_clears_claim(fake_storage_provider):
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    h = _make_harness(
        pending=HarnessOperation.FETCH,
        claimed_by="w1",
        last_heartbeat_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(Harness).create(h)
    await sched.release_harness(
        "h1", "w1", next_status=HarnessStatus.READY,
    )
    row = await fake_storage_provider.get_storage(Harness).get("h1")
    assert row.claimed_by is None
    assert row.claimed_at is None
    assert row.pending_operation is None
    assert row.status == HarnessStatus.READY


@pytest.mark.asyncio
async def test_release_records_error(fake_storage_provider):
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    h = _make_harness(
        pending=HarnessOperation.FETCH,
        claimed_by="w1",
        last_heartbeat_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(Harness).create(h)
    await sched.release_harness(
        "h1", "w1",
        next_status=HarnessStatus.ERROR,
        last_operation_error='{"code":"git_auth_failed"}',
    )
    row = await fake_storage_provider.get_storage(Harness).get("h1")
    assert row.status == HarnessStatus.ERROR
    assert row.last_operation_error is not None
