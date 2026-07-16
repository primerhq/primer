import asyncio
import pytest
from datetime import datetime, UTC, timedelta
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.claim.in_memory import InMemoryClaimEngine


@pytest.mark.asyncio
async def test_upsert_creates_lease_row():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "chat-1", priority=100)
    row = engine._leases[(ClaimKind.CHAT, "chat-1")]
    assert row.priority_score == 100
    assert row.claimed_by is None


@pytest.mark.asyncio
async def test_upsert_updates_existing_priority():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "chat-1", priority=100)
    await engine.upsert(ClaimKind.CHAT, "chat-1", priority=50)
    assert engine._leases[(ClaimKind.CHAT, "chat-1")].priority_score == 50


@pytest.mark.asyncio
async def test_delete_lease_removes_row():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "chat-1")
    await engine.delete_lease(ClaimKind.CHAT, "chat-1")
    assert (ClaimKind.CHAT, "chat-1") not in engine._leases


@pytest.mark.asyncio
async def test_claim_due_returns_eligible_leases_sorted_by_priority():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT,    "c1", priority=100)
    await engine.upsert(ClaimKind.SESSION, "s1", priority=50)   # higher priority
    await engine.upsert(ClaimKind.HARNESS, "h1", priority=10)   # highest

    leases = await engine.claim_due("worker-A", max_count=3)
    assert [l.entity_id for l in leases] == ["h1", "s1", "c1"]
    assert all(l.claimed_by == "worker-A" for l in leases)


@pytest.mark.asyncio
async def test_claim_due_respects_max_count():
    engine = InMemoryClaimEngine(adapters={})
    for i in range(5):
        await engine.upsert(ClaimKind.CHAT, f"c{i}")
    leases = await engine.claim_due("worker-A", max_count=2)
    assert len(leases) == 2


@pytest.mark.asyncio
async def test_claim_due_skips_claimed_leases():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "c1")
    first = await engine.claim_due("worker-A", max_count=1)
    second = await engine.claim_due("worker-B", max_count=1)
    assert len(first) == 1
    assert len(second) == 0  # already claimed by A


@pytest.mark.asyncio
async def test_claim_due_reclaims_expired_leases():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "c1")
    leases = await engine.claim_due("worker-A", max_count=1)
    # Force expiry
    engine._leases[(ClaimKind.CHAT, "c1")].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    reclaimed = await engine.claim_due("worker-B", max_count=1)
    assert len(reclaimed) == 1
    assert reclaimed[0].claimed_by == "worker-B"


@pytest.mark.asyncio
async def test_heartbeat_refreshes_expiry():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "c1")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    orig_expires = engine._leases[(ClaimKind.CHAT, "c1")].expires_at
    await asyncio.sleep(0.01)
    confirmed = await engine.heartbeat("worker-A", [(ClaimKind.CHAT, "c1")])
    assert confirmed == [(ClaimKind.CHAT, "c1")]
    assert engine._leases[(ClaimKind.CHAT, "c1")].expires_at > orig_expires


@pytest.mark.asyncio
async def test_heartbeat_rejects_non_owner():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "c1")
    await engine.claim_due("worker-A", max_count=1)
    confirmed = await engine.heartbeat("worker-B", [(ClaimKind.CHAT, "c1")])
    assert confirmed == []


@pytest.mark.asyncio
async def test_release_with_drop_lease_removes_row():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "c1")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert (ClaimKind.CHAT, "c1") not in engine._leases


@pytest.mark.asyncio
async def test_release_without_drop_makes_lease_reclaimable():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "c1")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=False))
    [again] = await engine.claim_due("worker-B", max_count=1)
    assert again.entity_id == "c1"
    assert again.claimed_by == "worker-B"


@pytest.mark.asyncio
async def test_release_bumps_attempt_count_on_failure():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.CHAT, "c1")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(
        success=False, last_error="boom", requeue_after=timedelta(seconds=10),
    ))
    row = engine._leases[(ClaimKind.CHAT, "c1")]
    assert row.attempt_count == 1
    assert row.last_error == "boom"
    assert row.next_attempt_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_mark_resumable_lowers_priority_and_wakes():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.SESSION, "s1", priority=100)
    await engine.mark_resumable(ClaimKind.SESSION, "s1", priority=50)
    row = engine._leases[(ClaimKind.SESSION, "s1")]
    assert row.priority_score == 50


@pytest.mark.asyncio
async def test_default_lease_ttl_is_60_seconds():
    engine = InMemoryClaimEngine(adapters={})
    assert engine.lease_ttl_seconds == 60
    await engine.upsert(ClaimKind.CHAT, "c1")
    before = datetime.now(UTC)
    [lease] = await engine.claim_due("worker-A", max_count=1)
    assert lease.expires_at is not None
    assert lease.expires_at >= before + timedelta(seconds=59)


@pytest.mark.asyncio
async def test_claim_due_honors_custom_lease_ttl():
    # A custom lease_ttl_seconds must drive the claimed lease's expiry
    # (A-I1: the configured TTL now actually reaches the engine).
    engine = InMemoryClaimEngine(adapters={}, lease_ttl_seconds=5)
    await engine.upsert(ClaimKind.CHAT, "c1")
    before = datetime.now(UTC)
    [lease] = await engine.claim_due("worker-A", max_count=1)
    after = datetime.now(UTC)
    assert lease.expires_at is not None
    assert before + timedelta(seconds=5) <= lease.expires_at <= after + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_heartbeat_honors_custom_lease_ttl():
    engine = InMemoryClaimEngine(adapters={}, lease_ttl_seconds=5)
    await engine.upsert(ClaimKind.CHAT, "c1")
    await engine.claim_due("worker-A", max_count=1)
    before = datetime.now(UTC)
    await engine.heartbeat("worker-A", [(ClaimKind.CHAT, "c1")])
    after = datetime.now(UTC)
    exp = engine._leases[(ClaimKind.CHAT, "c1")].expires_at
    assert exp is not None
    assert before + timedelta(seconds=5) <= exp <= after + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_watch_ready_yields_on_upsert():
    engine = InMemoryClaimEngine(adapters={})
    gen = engine.watch_ready()
    task = asyncio.create_task(anext(gen))
    await asyncio.sleep(0.01)
    await engine.upsert(ClaimKind.CHAT, "c1")
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == (ClaimKind.CHAT, "c1")
