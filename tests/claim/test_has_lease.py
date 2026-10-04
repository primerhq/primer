"""ClaimEngine.has_lease: does a lease ROW exist, claimed or not?

The stuck-session sweeper needs to tell a session whose claim was LOST (no row) from one
that is queued behind a busy pool or awaiting reclaim after its worker died (a row exists).
``has_live_lease`` cannot: it is False for an armed lease nobody has claimed yet, and False
again the moment a claimed lease expires, although in both cases the claim loop will pick
the entity up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimEngine, ClaimKind, ReleaseOutcome


@pytest.mark.asyncio
async def test_no_row_before_the_lease_is_armed():
    engine = InMemoryClaimEngine(adapters={})
    assert await engine.has_lease(ClaimKind.SESSION, "s-1") is False


@pytest.mark.asyncio
async def test_an_armed_unclaimed_lease_counts_though_it_is_not_live():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.SESSION, "s-1")

    assert await engine.has_lease(ClaimKind.SESSION, "s-1") is True
    assert await engine.has_live_lease(ClaimKind.SESSION, "s-1") is False


@pytest.mark.asyncio
async def test_a_claimed_lease_counts_and_is_live():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.SESSION, "s-1")
    await engine.claim_due("worker-A", max_count=1)

    assert await engine.has_lease(ClaimKind.SESSION, "s-1") is True
    assert await engine.has_live_lease(ClaimKind.SESSION, "s-1") is True


@pytest.mark.asyncio
async def test_an_expired_claim_still_counts_and_the_claim_loop_reclaims_it():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.SESSION, "s-1")
    await engine.claim_due("worker-A", max_count=1)
    engine._leases[(ClaimKind.SESSION, "s-1")].expires_at = (
        datetime.now(UTC) - timedelta(seconds=1)
    )

    assert await engine.has_live_lease(ClaimKind.SESSION, "s-1") is False
    assert await engine.has_lease(ClaimKind.SESSION, "s-1") is True
    reclaimed = await engine.claim_due("worker-B", max_count=1)
    assert [(lease.entity_id, lease.claimed_by) for lease in reclaimed] == [
        ("s-1", "worker-B"),
    ]


@pytest.mark.asyncio
async def test_the_row_is_gone_after_a_drop_lease_release_or_a_delete():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.SESSION, "s-1")
    (lease,) = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(success=False, drop_lease=True))
    assert await engine.has_lease(ClaimKind.SESSION, "s-1") is False

    await engine.upsert(ClaimKind.SESSION, "s-2")
    await engine.delete_lease(ClaimKind.SESSION, "s-2")
    assert await engine.has_lease(ClaimKind.SESSION, "s-2") is False


@pytest.mark.asyncio
async def test_the_answer_is_per_kind_and_per_entity():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.HARNESS, "x")

    assert await engine.has_lease(ClaimKind.HARNESS, "x") is True
    assert await engine.has_lease(ClaimKind.SESSION, "x") is False
    assert await engine.has_lease(ClaimKind.HARNESS, "y") is False


@pytest.mark.asyncio
async def test_an_engine_that_cannot_answer_says_yes_so_callers_do_nothing():
    """The ABC default is deliberately conservative: callers act destructively on 'no'."""

    class _Bare(ClaimEngine):
        async def claim_due(self, worker_id, *, max_count, kinds=None): return []
        async def heartbeat(self, worker_id, kind_ids): return []
        async def release(self, lease, *, outcome): ...
        async def mark_resumable(self, kind, entity_id, *, priority=50): ...
        async def watch_ready(self): yield  # pragma: no cover
        async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None): ...
        async def delete_lease(self, kind, entity_id): ...

    assert await _Bare().has_lease(ClaimKind.SESSION, "anything") is True
