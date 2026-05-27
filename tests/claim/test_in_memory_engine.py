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
