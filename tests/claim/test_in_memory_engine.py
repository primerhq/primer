import pytest
from datetime import datetime, UTC, timedelta
from matrix.int.claim import ClaimKind
from matrix.claim.in_memory import InMemoryClaimEngine


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
