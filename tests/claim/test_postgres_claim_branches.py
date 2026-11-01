"""claim_due over a mixed set of leases (perf plan Task 3, finding H4). Live Postgres only (``tests/pg_gate.py``).

Characterization: these pin what the claim query did before it was split into unclaimed and expired branches, and must pass unchanged after it:
the same leases are claimed, in ``priority_score, next_attempt_at`` order, with ``max_count`` applied across both kinds of candidate, an expired
lease reclaimed, a live or not-yet-due one left alone, and two claimers never taking the same lease.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimAdapter, ClaimKind
from tests.claim._entity_seed import EntitySeeder
from tests.claim.test_postgres_engine import _needs_pg, pg_storage  # noqa: F401  (the fixture and the gate the other live claim tests use)


class _AlwaysEligible(ClaimAdapter):
    kind = ClaimKind.SESSION
    entity_table = "sessions"

    def eligibility_sql(self) -> str:
        return "l.kind IS NOT NULL"

    async def on_release(self, conn, entity_id, *, outcome): ...


async def _world(pg_storage, ids):
    seeder = EntitySeeder(pg_storage)
    await seeder.seed("sessions", ids)
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters={ClaimKind.SESSION: _AlwaysEligible()})
    return engine, seeder


async def _set(pg_storage, entity_id, **cols):
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(cols))
    async with pg_storage.pool.acquire() as conn:
        await conn.execute(f"UPDATE {pg_storage.leases_table} SET {sets} WHERE kind = 'session' AND entity_id = $1", entity_id, *cols.values())


async def _expire(pg_storage, entity_id, by="w-dead"):
    now = datetime.now(UTC)
    await _set(pg_storage, entity_id, claimed_by=by, claimed_at=now - timedelta(minutes=5), expires_at=now - timedelta(minutes=1))


@_needs_pg
@pytest.mark.asyncio
async def test_expired_and_unclaimed_leases_are_claimed_in_priority_order_and_live_or_future_ones_are_left(pg_storage):
    ids = ["unc-5", "unc-7", "exp-1", "exp-6", "live", "future"]
    engine, seeder = await _world(pg_storage, ids)
    try:
        for entity_id, priority in zip(ids, (5, 7, 1, 6, 2, 3)):
            await engine.upsert(ClaimKind.SESSION, entity_id, priority=priority)
        await _expire(pg_storage, "exp-1")
        await _expire(pg_storage, "exp-6")
        now = datetime.now(UTC)
        await _set(pg_storage, "live", claimed_by="w-live", claimed_at=now, expires_at=now + timedelta(minutes=5))
        await _set(pg_storage, "future", next_attempt_at=now + timedelta(hours=1))

        claimed = await engine.claim_due("w-new", max_count=10)

        assert sorted(lse.entity_id for lse in claimed) == ["exp-1", "exp-6", "unc-5", "unc-7"]  # RETURNING order is not the claim order
        assert all(lse.claimed_by == "w-new" for lse in claimed)
        async with pg_storage.pool.acquire() as conn:
            holder = {r["entity_id"]: r["claimed_by"] for r in await conn.fetch(f"SELECT entity_id, claimed_by FROM {pg_storage.leases_table}")}
        assert holder["live"] == "w-live" and holder["future"] is None
    finally:
        await seeder.cleanup()


@_needs_pg
@pytest.mark.asyncio
async def test_max_count_is_applied_across_unclaimed_and_expired_candidates(pg_storage):
    """Two unclaimed (priority 5, 7) and two expired (1, 6): the three best overall are 1, 5, 6."""
    ids = ["unc-5", "unc-7", "exp-1", "exp-6"]
    engine, seeder = await _world(pg_storage, ids)
    try:
        for entity_id, priority in zip(ids, (5, 7, 1, 6)):
            await engine.upsert(ClaimKind.SESSION, entity_id, priority=priority)
        await _expire(pg_storage, "exp-1")
        await _expire(pg_storage, "exp-6")
        claimed = await engine.claim_due("w-new", max_count=3)
        assert sorted(lse.entity_id for lse in claimed) == ["exp-1", "exp-6", "unc-5"]
    finally:
        await seeder.cleanup()


@_needs_pg
@pytest.mark.asyncio
async def test_equal_priorities_are_claimed_oldest_attempt_first(pg_storage):
    ids = ["late", "early", "middle"]
    engine, seeder = await _world(pg_storage, ids)
    try:
        now = datetime.now(UTC)
        for entity_id, ago in (("late", 1), ("early", 30), ("middle", 10)):
            await engine.upsert(ClaimKind.SESSION, entity_id, priority=10, next_attempt_at=now - timedelta(minutes=ago))
        first_two = await engine.claim_due("w-new", max_count=2)
        assert sorted(lse.entity_id for lse in first_two) == ["early", "middle"]
    finally:
        await seeder.cleanup()


@_needs_pg
@pytest.mark.asyncio
async def test_an_expired_lease_is_reclaimed_by_a_new_worker(pg_storage):
    engine, seeder = await _world(pg_storage, ["s-1"])
    try:
        await engine.upsert(ClaimKind.SESSION, "s-1")
        [first] = await engine.claim_due("w-old", max_count=1)
        assert first.claimed_by == "w-old" and await engine.claim_due("w-other", max_count=1) == []
        await _expire(pg_storage, "s-1", by="w-old")
        [again] = await engine.claim_due("w-new", max_count=1)
        assert again.entity_id == "s-1" and again.claimed_by == "w-new"
    finally:
        await seeder.cleanup()


@_needs_pg
@pytest.mark.asyncio
async def test_two_concurrent_claimers_never_take_the_same_lease(pg_storage):
    ids = [f"s-{i:02d}" for i in range(20)]
    engine, seeder = await _world(pg_storage, ids)
    try:
        for i, entity_id in enumerate(ids):
            await engine.upsert(ClaimKind.SESSION, entity_id, priority=i)
        for entity_id in ids[10:]:
            await _expire(pg_storage, entity_id)  # half unclaimed, half expired
        a, b = await asyncio.gather(engine.claim_due("w-a", max_count=20), engine.claim_due("w-b", max_count=20))
        taken_a, taken_b = {lse.entity_id for lse in a}, {lse.entity_id for lse in b}
        assert not (taken_a & taken_b), "a lease was claimed by both workers"
        assert taken_a | taken_b == set(ids)
    finally:
        await seeder.cleanup()


@_needs_pg
@pytest.mark.asyncio
async def test_the_claim_indexes_exist(pg_storage):
    async with pg_storage.pool.acquire() as conn:
        rows = await conn.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1 AND tablename = 'leases'", pg_storage.schema)
    defs = {r["indexname"]: r["indexdef"] for r in rows}
    assert "(kind, priority_score, next_attempt_at)" in defs["leases_unclaimed_by_kind"] and "claimed_by IS NULL" in defs["leases_unclaimed_by_kind"]
    assert "(kind, expires_at)" in defs["leases_expiry_by_kind"] and "claimed_by IS NOT NULL" in defs["leases_expiry_by_kind"]
