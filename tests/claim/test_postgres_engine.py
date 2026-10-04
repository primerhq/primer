"""Tests for PostgresClaimEngine — upsert + delete_lease + claim_due +
heartbeat + release + mark_resumable + watch_ready.

Live Postgres tests require PRIMER_TEST_POSTGRES_URL and are skipped otherwise.
Pure SQL-builder unit tests (``test_build_claim_query_*``) run without any
database and are never skipped.

The fixture sets up a fresh PostgresStorageProvider (which creates the
leases table), pre-seeds any entity rows needed for claim_due tests,
then cleans up on exit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.postgres import PostgresClaimEngine
from primer.claim.sql import build_claim_query
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.claim._entity_seed import EntitySeeder
from tests.pg_gate import CANONICAL_ENV, explicit_port, needs_postgres, require_postgres_url


_URL_ENV = CANONICAL_ENV

# Convenience decorator applied to each test that needs a live database.
_needs_pg = needs_postgres("Postgres claim-engine tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_url(url: str) -> PostgresConfig:
    from primer.model.except_ import ConfigError

    p = urlparse(url)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_URL_ENV}")
    query = parse_qs(p.query)
    schema = query.get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=explicit_port(p),
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_storage() -> AsyncIterator[PostgresStorageProvider]:
    """Initialised PostgresStorageProvider; cleans up leases on entry/exit."""
    url = require_postgres_url("Postgres claim-engine tests")

    cfg = _parse_url(url)
    sp = PostgresStorageProvider(cfg)
    await sp.initialize()

    # Start each test with empty leases table.
    async with sp.pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {sp.leases_table}")

    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {sp.leases_table}")
        await sp.aclose()


@pytest_asyncio.fixture
async def pg_engine(pg_storage: PostgresStorageProvider) -> PostgresClaimEngine:
    """PostgresClaimEngine with real adapters (storage=None for unit scope)."""
    adapters = {
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
    }
    return PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)


@pytest_asyncio.fixture
async def entity_seeder(pg_storage: PostgresStorageProvider) -> AsyncIterator[EntitySeeder]:
    """Seeds the entity rows claim_due's INNER JOIN needs; removes them on exit."""
    seeder = EntitySeeder(pg_storage)
    try:
        yield seeder
    finally:
        await seeder.cleanup()


# ---------------------------------------------------------------------------
# Tests — upsert
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_upsert_creates_row(pg_engine, pg_storage):
    await pg_engine.upsert(ClaimKind.HARNESS, "c-1", priority=100)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'c-1'"
        )

    assert row is not None
    assert row["priority_score"] == 100
    assert row["claimed_by"] is None


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_upsert_updates_priority(pg_engine, pg_storage):
    await pg_engine.upsert(ClaimKind.HARNESS, "c-1", priority=100)
    await pg_engine.upsert(ClaimKind.HARNESS, "c-1", priority=50)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT priority_score FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'c-1'"
        )

    assert row["priority_score"] == 50


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_upsert_preserves_next_attempt_when_null(pg_engine, pg_storage):
    """Re-upserting without next_attempt_at should preserve the existing value."""
    from datetime import datetime, UTC, timedelta

    future = datetime.now(UTC) + timedelta(hours=1)
    await pg_engine.upsert(ClaimKind.SESSION, "s-1", priority=100, next_attempt_at=future)

    # Second upsert with no next_attempt_at should not reset the timestamp.
    await pg_engine.upsert(ClaimKind.SESSION, "s-1", priority=80)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT next_attempt_at FROM {pg_storage.leases_table} "
            f"WHERE kind = 'session' AND entity_id = 's-1'"
        )

    # The stored value should still be >= future (within a reasonable delta).
    from datetime import UTC
    stored = row["next_attempt_at"].replace(tzinfo=UTC)
    assert stored >= future - timedelta(seconds=1)


# ---------------------------------------------------------------------------
# Tests — delete_lease
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_delete_lease_removes_row(pg_engine, pg_storage):
    await pg_engine.upsert(ClaimKind.HARNESS, "c-del", priority=100)
    await pg_engine.delete_lease(ClaimKind.HARNESS, "c-del")

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT 1 FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'c-del'"
        )

    assert row is None


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_delete_lease_noop_on_missing(pg_engine):
    # Must not raise.
    await pg_engine.delete_lease(ClaimKind.HARNESS, "not-there")


# ---------------------------------------------------------------------------
# Tests — claim_due
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_claim_due_with_no_adapters_returns_empty(pg_storage):
    """claim_due with no adapters produces no-op SQL; returns empty list.

    The no-op query used to omit $1, so this raised asyncpg's
    IndeterminateDatatypeError instead of returning [].
    """
    bare_engine = PostgresClaimEngine(
        storage_provider=pg_storage,
        adapters={},
    )
    await bare_engine.upsert(ClaimKind.HARNESS, "c-bare")
    leases = await bare_engine.claim_due("worker-A", max_count=5)
    # No adapters → no CTEs → no rows claimed.
    assert leases == []


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_claim_due_respects_max_count(pg_storage, entity_seeder):
    """Seed multiple leases; claim_due should respect max_count.

    Uses a synthetic adapter whose eligibility SQL only touches the lease
    alias. The claim query still INNER JOINs the adapter's entity table, so
    an entity row is seeded for every lease (a lease without one is never
    claimable).
    """
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            # Always-true fragment referencing only the lease row alias.
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )

    await entity_seeder.seed(adapter.entity_table, [f"c-{i}" for i in range(5)])
    for i in range(5):
        await engine.upsert(ClaimKind.HARNESS, f"c-{i}")

    leases = await engine.claim_due("worker-A", max_count=3)
    assert len(leases) == 3
    assert all(lse.claimed_by == "worker-A" for lse in leases)


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_claim_due_skips_already_claimed(pg_storage, entity_seeder):
    """A lease already claimed (within TTL) should not be returned again."""
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.SESSION
        entity_table = "sessions"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.SESSION: adapter},
    )

    await entity_seeder.seed(adapter.entity_table, ["s-1"])
    await engine.upsert(ClaimKind.SESSION, "s-1")

    first = await engine.claim_due("worker-A", max_count=1)
    assert len(first) == 1

    second = await engine.claim_due("worker-B", max_count=1)
    assert second == []


# ---------------------------------------------------------------------------
# Tests — build_claim_query (unit, no DB, always run)
# ---------------------------------------------------------------------------


def test_build_claim_query_empty_adapters():
    """With no adapters, the returned SQL is a no-op UPDATE."""
    sql = build_claim_query({}, '"test"."leases"')
    # Should contain the WITH + UPDATE skeleton.
    assert "WITH" in sql
    assert "UPDATE" in sql
    # No adapter CTEs.
    assert "harness_cand" not in sql
    assert "session_cand" not in sql


@pytest.mark.parametrize(
    "adapters",
    [
        {},
        {ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None)},
        {
            ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
        },
    ],
    ids=["no-adapters", "one-adapter", "two-adapters"],
)
def test_build_claim_query_references_every_bound_parameter(adapters):
    """claim_due ALWAYS binds $1 (max_count), $2 (worker_id) and $3 (ttl).

    Postgres cannot infer a type for a bound parameter that appears nowhere
    in the statement (asyncpg raises IndeterminateDatatypeError), so every
    shape of the query - including the zero-adapter no-op - must reference
    all three. The no-op branch used to omit $1.
    """
    sql = build_claim_query(adapters, '"test"."leases"')
    for n in (1, 2, 3):
        assert f"${n}" in sql, f"${n} is bound by claim_due but unused in the SQL"


def test_build_claim_query_single_adapter():
    adapters = {ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None)}
    sql = build_claim_query(adapters, '"test"."leases"')

    assert "harness_cand" in sql
    assert "harness" in sql
    # Only one CTE — no union needed.
    assert "UNION ALL" not in sql


def test_build_claim_query_multiple_adapters():
    adapters = {
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
    }
    sql = build_claim_query(adapters, '"test"."leases"')

    assert "harness_cand" in sql
    assert "session_cand" in sql
    assert "UNION ALL" in sql
    assert "RETURNING" in sql


def test_build_claim_query_schema_qualifies_entity_tables():
    """When schema is provided, entity table JOINs use schema-qualified names."""
    adapters = {ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None)}
    sql = build_claim_query(adapters, '"myschema"."leases"', schema="myschema")

    assert '"myschema"."harness"' in sql


# ---------------------------------------------------------------------------
# Tests — PostgresClaimEngine._claim_query_for (unit, no DB, always run)
# ---------------------------------------------------------------------------


class _StorageProviderStub:
    """Bare-minimum stand-in - PostgresClaimEngine.__init__ only reads
    these two attributes; no live connection is touched until claim_due
    itself runs, so _claim_query_for is fully testable without a DB."""

    def __init__(self, *, schema: str | None = "test") -> None:
        self.leases_table = '"test"."leases"'
        self.schema = schema


def test_claim_query_for_none_returns_the_prebuilt_all_kinds_query():
    engine = PostgresClaimEngine(
        storage_provider=_StorageProviderStub(),
        adapters={
            ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
        },
    )
    assert engine._claim_query_for(None) is engine._claim_query
    assert "harness_cand" in engine._claim_query_for(None)
    assert "session_cand" in engine._claim_query_for(None)


def test_claim_query_for_kinds_scopes_to_the_subset_and_caches():
    """Phase 3 stage 7a (01a0518b) pool-class separation: the pool loop
    calls claim_due twice per iteration with different kind subsets -
    each subset's compiled query must only contain THAT subset's CTEs,
    and a repeated call for the same subset must hit the cache rather
    than rebuild."""
    engine = PostgresClaimEngine(
        storage_provider=_StorageProviderStub(),
        adapters={
            ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
        },
    )
    scoped = engine._claim_query_for([ClaimKind.HARNESS])
    assert "harness_cand" in scoped
    assert "session_cand" not in scoped
    assert "UNION ALL" not in scoped

    # Repeat call with an equal (but distinct list object) subset hits
    # the cache - same compiled string, not merely an equal one.
    again = engine._claim_query_for([ClaimKind.HARNESS])
    assert again is scoped

    # A different subset gets its own, independently-cached query.
    other = engine._claim_query_for([ClaimKind.SESSION])
    assert "session_cand" in other
    assert "harness_cand" not in other
    assert other is not scoped


# ---------------------------------------------------------------------------
# Tests — heartbeat
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_heartbeat_refreshes_expiry(pg_storage, entity_seeder):
    """heartbeat extends expires_at and confirms the (kind, entity_id) pair."""
    from datetime import UTC, timedelta

    # Use the no-join adapter trick so no entity row is needed.
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )

    await entity_seeder.seed(adapter.entity_table, ["hb-1"])
    await engine.upsert(ClaimKind.HARNESS, "hb-1")
    [lease] = await engine.claim_due("worker-A", max_count=1)

    # Record the expires_at BEFORE heartbeat.
    async with pg_storage.pool.acquire() as conn:
        before_row = await conn.fetchrow(
            f"SELECT expires_at FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'hb-1'"
        )
    import asyncio
    await asyncio.sleep(0.05)

    confirmed = await engine.heartbeat("worker-A", [(ClaimKind.HARNESS, "hb-1")])
    assert confirmed == [(ClaimKind.HARNESS, "hb-1")]

    async with pg_storage.pool.acquire() as conn:
        after_row = await conn.fetchrow(
            f"SELECT expires_at FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'hb-1'"
        )
    assert after_row["expires_at"] >= before_row["expires_at"]


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_heartbeat_rejects_non_owner(pg_storage, entity_seeder):
    """heartbeat with the wrong worker_id returns an empty list.

    The lease must actually be claimed by worker A first: with nothing
    claimed, worker B's heartbeat is empty whether or not the ownership
    check exists, and the test would pass without testing it (it did, until
    the claim itself started succeeding). Worker A's own heartbeat is the
    positive control proving the lease is really held.
    """
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.SESSION
        entity_table = "sessions"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.SESSION: adapter},
    )

    await entity_seeder.seed(adapter.entity_table, ["hb-wrong"])
    await engine.upsert(ClaimKind.SESSION, "hb-wrong")
    claimed = await engine.claim_due("worker-A", max_count=1)
    assert [(c.kind, c.entity_id, c.claimed_by) for c in claimed] == [
        (ClaimKind.SESSION, "hb-wrong", "worker-A"),
    ], "precondition: worker-A must really hold the lease"

    pair = [(ClaimKind.SESSION, "hb-wrong")]
    # Positive control: the owner's heartbeat IS confirmed ...
    assert await engine.heartbeat("worker-A", pair) == pair
    # ... and a different worker's is rejected.
    assert await engine.heartbeat("worker-B", pair) == []


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_heartbeat_empty_list(pg_engine):
    """heartbeat with no pairs is a fast path — returns empty list."""
    result = await pg_engine.heartbeat("worker-A", [])
    assert result == []


# ---------------------------------------------------------------------------
# Tests — release
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_release_drop_lease_deletes_row(pg_storage, entity_seeder):
    """release with drop_lease=True removes the lease row."""
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )

    await entity_seeder.seed(adapter.entity_table, ["rel-drop"])
    await engine.upsert(ClaimKind.HARNESS, "rel-drop")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT 1 FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'rel-drop'"
        )
    assert row is None


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_release_with_a_stale_claimed_at_still_runs_on_release(
    pg_storage, entity_seeder,
):
    """The release fence is ``claimed_by`` only; ``claimed_at`` is NOT part of it.

    A worker's heartbeat stalls, the lease expires, and the SAME worker claims the row again
    (``worker_id`` is per pool start, and the pool skips the duplicate, so the first execution
    is still the only one). Its release carries the OLD ``claimed_at`` and must still run
    ``on_release``: a ``claimed_at`` term would drop the park write or the tool result.
    """
    from primer.int.claim import ClaimAdapter

    calls: list[str] = []

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )

    await entity_seeder.seed(adapter.entity_table, ["rel-stale"])
    await engine.upsert(ClaimKind.HARNESS, "rel-stale")
    [first] = await engine.claim_due("worker-A", max_count=1)
    async with pg_storage.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {pg_storage.leases_table} SET expires_at = now() - interval '1 second' "
            f"WHERE kind = 'harness' AND entity_id = 'rel-stale'"
        )
    [second] = await engine.claim_due("worker-A", max_count=1)
    assert second.claimed_by == first.claimed_by == "worker-A"
    assert second.claimed_at != first.claimed_at

    await engine.release(first, outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert calls == ["rel-stale"], "a release with an older claimed_at was fenced out"
    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT 1 FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'rel-stale'"
        )
    assert row is None


@_needs_pg
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ClaimKind.SESSION, ClaimKind.HARNESS])
async def test_postgres_pool_same_worker_reclaim_neither_preempts_nor_redispatches(
    kind, pg_storage, entity_seeder,
):
    """Pool level on the Postgres engine: see tests/worker/_duplicate_claim_scenario.py."""
    from primer.int.claim import ClaimAdapter
    from primer.model.scheduler import WorkerConfig
    from primer.scheduler.in_memory import InMemoryScheduler
    from primer.worker.pool import WorkerPool
    from tests.worker._duplicate_claim_scenario import run_reclaim_scenario

    released: list[str] = []

    class _SpyAdapter(ClaimAdapter):
        entity_table = "dupclaim_spy"

        def __init__(self, k: ClaimKind) -> None:
            self.kind = k

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome):
            released.append(entity_id)

    adapter = _SpyAdapter(kind)
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters={kind: adapter})
    await entity_seeder.seed(adapter.entity_table, ["dup", "other"])
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=4, claim_batch_size=4, heartbeat_interval_seconds=1,
            lease_ttl_seconds=5, poll_interval_seconds=0.1, drain_timeout_seconds=5,
        ),
        scheduler=scheduler, storage=pg_storage,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=engine,
    )

    async def _force_expired(k: ClaimKind, entity_id: str) -> None:
        async with pg_storage.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {pg_storage.leases_table} SET expires_at = now() - interval '1 second' "
                f"WHERE kind = $1 AND entity_id = $2",
                k.value, entity_id,
            )

    async def _lease_state(k: ClaimKind, entity_id: str):
        async with pg_storage.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT claimed_by, claimed_at FROM {pg_storage.leases_table} "
                f"WHERE kind = $1 AND entity_id = $2",
                k.value, entity_id,
            )
        return None if row is None else (row["claimed_by"], row["claimed_at"])

    try:
        await run_reclaim_scenario(
            kind=kind, pool=pool, engine=engine, released=released,
            ids=("dup", "other"), force_expired=_force_expired, lease_state=_lease_state,
        )
    finally:
        await scheduler.aclose()


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_entity_noop_release_skips_on_release_and_only_moves_the_lease(
    pg_storage, entity_seeder,
):
    """A lease-only release: ``on_release`` is not called (the entity is neither read nor written);
    the lease is requeued (claimable again; ``attempt_count`` and ``last_error`` left alone, which is the whole
    difference from a success release that resets them) or dropped."""
    from primer.int.claim import ClaimAdapter

    calls: list[str] = []

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )
    await entity_seeder.seed(adapter.entity_table, ["noop-requeue", "noop-drop"])
    await engine.upsert(ClaimKind.HARNESS, "noop-requeue")
    await engine.upsert(ClaimKind.HARNESS, "noop-drop")
    leases = {x.entity_id: x for x in await engine.claim_due("worker-A", max_count=8)}
    assert set(leases) == {"noop-requeue", "noop-drop"}

    await engine.release(leases["noop-requeue"], outcome=ReleaseOutcome(success=True, entity_noop=True))
    await engine.release(
        leases["noop-drop"],
        outcome=ReleaseOutcome(success=True, entity_noop=True, drop_lease=True),
    )

    assert calls == [], "entity_noop must not call on_release"
    async with pg_storage.pool.acquire() as conn:
        rows = {
            r["entity_id"]: r
            for r in await conn.fetch(
                f"SELECT entity_id, claimed_by, attempt_count, last_error FROM {pg_storage.leases_table} "
                f"WHERE kind = 'harness'"
            )
        }
    assert set(rows) == {"noop-requeue"} and rows["noop-requeue"]["claimed_by"] is None
    again = await engine.claim_due("worker-B", max_count=8)
    assert [x.entity_id for x in again] == ["noop-requeue"]


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_entity_noop_requeue_leaves_the_failure_history_and_place_in_line_alone(
    pg_storage, entity_seeder,
):
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )
    await entity_seeder.seed(adapter.entity_table, ["noop-hist"])
    await engine.upsert(ClaimKind.HARNESS, "noop-hist")
    for _ in range(3):
        [lease] = await engine.claim_due("worker-A", max_count=1)
        await engine.release(lease, outcome=ReleaseOutcome(success=False, last_error="boom"))
        async with pg_storage.pool.acquire() as conn:      # make the failed requeue claimable at once
            await conn.execute(
                f"UPDATE {pg_storage.leases_table} SET next_attempt_at = now() - interval '1 second' "
                f"WHERE entity_id = 'noop-hist'"
            )
    async with pg_storage.pool.acquire() as conn:
        before = await conn.fetchrow(
            f"SELECT attempt_count, last_error, next_attempt_at FROM {pg_storage.leases_table} "
            f"WHERE entity_id = 'noop-hist'"
        )
    assert (before["attempt_count"], before["last_error"]) == (3, "boom")

    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(success=True, entity_noop=True))

    async with pg_storage.pool.acquire() as conn:
        after = await conn.fetchrow(
            f"SELECT claimed_by, attempt_count, last_error, next_attempt_at FROM {pg_storage.leases_table} "
            f"WHERE entity_id = 'noop-hist'"
        )
    assert after["claimed_by"] is None
    assert (after["attempt_count"], after["last_error"]) == (3, "boom")
    assert after["next_attempt_at"] == before["next_attempt_at"]


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_release_without_drop_clears_claim_fields(pg_storage, entity_seeder):
    """release without drop_lease clears claimed_by and makes row reclaimable."""
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )

    await entity_seeder.seed(adapter.entity_table, ["rel-clear"])
    await engine.upsert(ClaimKind.HARNESS, "rel-clear")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=False))

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT claimed_by, attempt_count FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'rel-clear'"
        )
    assert row is not None
    assert row["claimed_by"] is None
    assert row["attempt_count"] == 0


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_release_failure_bumps_attempt_count(pg_storage, entity_seeder):
    """release with success=False increments attempt_count and stores last_error."""
    from datetime import timedelta
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )

    await entity_seeder.seed(adapter.entity_table, ["rel-fail"])
    await engine.upsert(ClaimKind.HARNESS, "rel-fail")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(
        lease,
        outcome=ReleaseOutcome(
            success=False,
            last_error="something went wrong",
            requeue_after=timedelta(seconds=30),
        ),
    )

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT attempt_count, last_error, next_attempt_at FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'rel-fail'"
        )
    from datetime import datetime, UTC
    assert row is not None
    assert row["attempt_count"] == 1
    assert row["last_error"] == "something went wrong"
    # next_attempt_at should be in the future (requeue_after=30s).
    assert row["next_attempt_at"].replace(tzinfo=UTC) > datetime.now(UTC)


# ---------------------------------------------------------------------------
# Tests — on_release transaction integration (chat adapter)
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_release_on_release_runs_in_transaction(pg_storage):
    """release calls adapter.on_release inside the same transaction.

    Scenario: create a Harness entity with a pending operation, upsert +
    claim its lease, release with drop_lease=True, then verify the lease
    row is gone AND on_release cleared ``pending_operation``.
    """
    from datetime import datetime, UTC
    from primer.model.harness import Harness, HarnessOperation, HarnessStatus
    from primer.storage.postgres import PostgresStorage

    from primer.int.storage import Storage
    harness_storage: Storage[Harness] = pg_storage.get_storage(Harness)

    harness = Harness(
        id="txn-harness-1",
        slug="txn-harness",
        name="txn harness",
        created_at=datetime.now(UTC),
        status=HarnessStatus.DRAFT,
        pending_operation=HarnessOperation.INSTALL,
    )
    await harness_storage.create(harness)
    chat = harness
    chat_storage = harness_storage

    try:
        adapter = HarnessClaimAdapter(harness_storage=harness_storage)

        class _EligibleAdapter(type(adapter)):
            """Override eligibility so no extra entity state is required."""
            def eligibility_sql(self) -> str:
                return "l.kind IS NOT NULL"

        real_adapter = _EligibleAdapter(harness_storage=harness_storage)
        adapters = {ClaimKind.HARNESS: real_adapter}
        engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

        await engine.upsert(ClaimKind.HARNESS, chat.id)
        [lease] = await engine.claim_due("worker-txn", max_count=1)

        # Release with drop_lease=True + success=True -> on_release should set turn_status='idle'.
        await engine.release(
            lease,
            outcome=ReleaseOutcome(success=True, drop_lease=True),
        )

        # Verify lease row is gone.
        async with pg_storage.pool.acquire() as conn:
            lease_row = await conn.fetchrow(
                f"SELECT 1 FROM {pg_storage.leases_table} "
                f"WHERE kind = 'harness' AND entity_id = $1",
                chat.id,
            )
        assert lease_row is None, "Lease row should have been deleted"

        # Verify on_release cleared the pending operation.
        updated = await harness_storage.get(harness.id)
        assert updated is not None
        assert updated.pending_operation is None, (
            f"Expected pending_operation cleared, got {updated.pending_operation!r}"
        )

    finally:
        # Clean up the entity row.
        try:
            await chat_storage.delete(chat.id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests - fenced release (skip on_release when claimed_by changed)
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_release_fenced_when_reclaimed_by_other_worker(
    pg_storage, caplog,
):
    """A's release no-ops when worker B re-claimed the lease in between.

    Scenario: worker A claims the lease, then worker B re-claims it
    (simulated by updating ``claimed_by`` in the leases table). When A
    then calls ``release`` the fence (``claimed_by = $owner``) matches no
    row, so neither the lease row NOR the entity row is mutated by A's
    release, and a warning is logged. This is the live-Postgres analogue
    of the in-memory fenced-release unit test.
    """
    import logging
    from datetime import datetime, UTC
    from primer.int.storage import Storage
    from primer.model.harness import Harness, HarnessOperation, HarnessStatus

    harness_storage: Storage[Harness] = pg_storage.get_storage(Harness)
    harness = Harness(
        id="fenced-harness-1",
        slug="fenced-harness",
        name="fenced harness",
        created_at=datetime.now(UTC),
        status=HarnessStatus.DRAFT,
        pending_operation=HarnessOperation.INSTALL,
    )
    await harness_storage.create(harness)
    chat = harness
    chat_storage = harness_storage

    try:
        adapter = HarnessClaimAdapter(harness_storage=harness_storage)

        class _EligibleAdapter(type(adapter)):
            """Override eligibility so no extra entity state is required."""
            def eligibility_sql(self) -> str:
                return "l.kind IS NOT NULL"

        real_adapter = _EligibleAdapter(harness_storage=harness_storage)
        adapters = {ClaimKind.HARNESS: real_adapter}
        engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

        await engine.upsert(ClaimKind.HARNESS, chat.id)
        [lease_a] = await engine.claim_due("worker-A", max_count=1)
        assert lease_a.claimed_by == "worker-A"

        # Simulate worker B re-claiming the lease: flip claimed_by to B
        # directly in the leases table (as a real second worker's
        # claim_due would have done).
        async with pg_storage.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {pg_storage.leases_table} "
                f"   SET claimed_by = 'worker-B' "
                f" WHERE kind = 'harness' AND entity_id = $1",
                chat.id,
            )

        # A's release must no-op (fence mismatch). Use drop_lease=True +
        # success=True so, absent the fence, it would delete the lease row
        # AND on_release would clear pending_operation.
        with caplog.at_level(logging.WARNING, logger="primer.claim.postgres"):
            await engine.release(
                lease_a,
                outcome=ReleaseOutcome(success=True, drop_lease=True),
            )

        # Lease row is UNCHANGED: still present and still owned by B.
        async with pg_storage.pool.acquire() as conn:
            lease_row = await conn.fetchrow(
                f"SELECT claimed_by FROM {pg_storage.leases_table} "
                f"WHERE kind = 'harness' AND entity_id = $1",
                chat.id,
            )
        assert lease_row is not None, "A's release should not delete B's lease"
        assert lease_row["claimed_by"] == "worker-B"

        # Entity row is UNCHANGED: on_release was skipped, so the pending
        # operation is still set.
        unchanged = await harness_storage.get(harness.id)
        assert unchanged is not None
        assert unchanged.pending_operation is HarnessOperation.INSTALL

        # A warning was logged about the skipped (fenced) release.
        assert any(
            "release skipped" in rec.getMessage()
            for rec in caplog.records
        ), "expected a fenced-release warning"

    finally:
        try:
            await chat_storage.delete(chat.id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests — mark_resumable
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_mark_resumable_inserts_with_priority(pg_engine, pg_storage):
    """mark_resumable inserts a new row with the given priority."""
    await pg_engine.mark_resumable(ClaimKind.HARNESS, "mr-new", priority=30)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT priority_score, claimed_by FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'mr-new'"
        )
    assert row is not None
    assert row["priority_score"] == 30
    assert row["claimed_by"] is None


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_mark_resumable_updates_existing_priority(pg_engine, pg_storage):
    """mark_resumable lowers priority and resets next_attempt_at on conflict."""
    from datetime import datetime, UTC, timedelta

    future = datetime.now(UTC) + timedelta(hours=1)
    await pg_engine.upsert(ClaimKind.HARNESS, "mr-exist", priority=100, next_attempt_at=future)

    # mark_resumable should bump it to priority=25 and reset next_attempt_at to now.
    await pg_engine.mark_resumable(ClaimKind.HARNESS, "mr-exist", priority=25)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT priority_score, next_attempt_at FROM {pg_storage.leases_table} "
            f"WHERE kind = 'harness' AND entity_id = 'mr-exist'"
        )
    assert row is not None
    assert row["priority_score"] == 25
    # next_attempt_at should now be close to now(), not the future value.
    stored = row["next_attempt_at"].replace(tzinfo=UTC)
    assert stored < datetime.now(UTC) + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Tests — watch_ready
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_watch_ready_yields_on_upsert(pg_engine):
    """watch_ready yields (ClaimKind, entity_id) tuples when pg_notify fires."""
    import asyncio

    gen = pg_engine.watch_ready()

    async def consume_one():
        return await gen.__anext__()

    task = asyncio.create_task(consume_one())
    await asyncio.sleep(0.05)  # Let the listener subscribe.

    await pg_engine.upsert(ClaimKind.SESSION, "wr-1")
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result == (ClaimKind.SESSION, "wr-1")

    # Cleanup: close the generator.
    await gen.aclose()


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_watch_ready_yields_on_mark_resumable(pg_engine):
    """watch_ready also fires when mark_resumable notifies claim_ready."""
    import asyncio

    gen = pg_engine.watch_ready()

    async def consume_one():
        return await gen.__anext__()

    task = asyncio.create_task(consume_one())
    await asyncio.sleep(0.05)

    await pg_engine.mark_resumable(ClaimKind.HARNESS, "wr-mr-1", priority=40)
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result == (ClaimKind.HARNESS, "wr-mr-1")

    await gen.aclose()


# --- the LISTEN backend dying -----------------------------------------------
# asyncpg never raises into the NOTIFY queue when the backend goes away (a
# Postgres restart or failover, a network blip), so a watcher that does not
# listen for connection termination parks forever and the pool stops being
# woken for new claims. The no-DB twin is test_postgres_listen_drop.py.


async def _listen_pids(sp, channel: str) -> set[int]:
    async with sp.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT pid FROM pg_stat_activity "
            "WHERE pid <> pg_backend_pid() AND query ILIKE $1",
            f"%listen%{channel}%",
        )
    return {r["pid"] for r in rows}


async def _new_listen_pid(sp, channel: str, known: set[int], *, timeout: float = 10.0) -> int:
    """The pid of a LISTEN backend on ``channel`` that is not in ``known``."""
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        fresh = await _listen_pids(sp, channel) - known
        if fresh:
            assert len(fresh) == 1, f"expected exactly one new LISTEN backend, got {fresh}"
            return next(iter(fresh))
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"no new LISTEN backend on {channel} within {timeout}s")
        await asyncio.sleep(0.05)


async def _terminate_backend(sp, pid: int) -> None:
    async with sp.pool.acquire() as conn:
        assert await conn.fetchval("SELECT pg_terminate_backend($1)", pid)


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_watch_ready_raises_listen_connection_lost_when_its_backend_is_terminated(
    pg_engine, pg_storage,
):
    import asyncio

    from primer.model.except_ import ListenConnectionLost

    known = await _listen_pids(pg_storage, "claim_ready")

    async def consume():
        async for _ in pg_engine.watch_ready():
            pass

    task = asyncio.create_task(consume())
    try:
        pid = await _new_listen_pid(pg_storage, "claim_ready", known)
        await _terminate_backend(pg_storage, pid)
        with pytest.raises(ListenConnectionLost, match="claim_ready"):
            await asyncio.wait_for(task, timeout=5.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@_needs_pg
@pytest.mark.asyncio
async def test_engine_bus_loop_wakes_the_claim_loop_after_its_listen_backend_is_terminated(
    pg_engine, pg_storage,
):
    """The pool's restart arm re-subscribes, and a claim_ready sent AFTER the
    drop wakes the claim loop again (before the fix it never did)."""
    import asyncio

    from primer.model.scheduler import WorkerConfig
    from primer.worker.pool import WorkerPool

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, poll_interval_seconds=0.1),
        scheduler=None,                                # type: ignore[arg-type]
        storage=None,                                  # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
        engine=pg_engine,
    )

    async def wait_woken(what: str) -> None:
        for _ in range(250):
            if pool._wake.is_set():
                return
            await asyncio.sleep(0.02)
        pytest.fail(f"the claim loop was never woken: {what}")

    known = await _listen_pids(pg_storage, "claim_ready")
    task = asyncio.create_task(pool._engine_bus_loop())
    try:
        first = await _new_listen_pid(pg_storage, "claim_ready", known)
        await pg_engine.upsert(ClaimKind.SESSION, "loop-before-drop")
        await wait_woken("before the drop (control)")
        pool._wake.clear()

        await _terminate_backend(pg_storage, first)
        await _new_listen_pid(pg_storage, "claim_ready", known | {first})

        await pg_engine.upsert(ClaimKind.SESSION, "loop-after-drop")
        await wait_woken("after the drop")
    finally:
        pool._stopping.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Regression: claim_due must work on a fresh DB where entity tables for the
# claim kinds have not been created yet (lazily-created by storage on first
# write). The engine ensures them; the adapter table names match the storage
# convention (chat/harness/trigger/sessions, not plural).
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_claim_due_on_fresh_schema_ensures_entity_tables(pg_storage):
    from primer.claim.adapters.harnesses import HarnessClaimAdapter
    from primer.claim.adapters.triggers import TriggerClaimAdapter

    schema = pg_storage.schema
    # Simulate a fresh DB: drop the per-kind entity tables.
    async with pg_storage.pool.acquire() as conn:
        for t in ("harness", "trigger", "sessions"):
            await conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{t}" CASCADE')

    adapters = {
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
        ClaimKind.TRIGGER: TriggerClaimAdapter(storage=None),
    }
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

    # Must NOT raise UndefinedTableError even though no entity exists yet.
    leases = await engine.claim_due("w-fresh", max_count=5)
    assert leases == []

    # The engine created each entity table with the storage-convention name.
    async with pg_storage.pool.acquire() as conn:
        for t in ("harness", "trigger", "sessions"):
            reg = await conn.fetchval("SELECT to_regclass($1)", f"{schema}.{t}")
            assert reg is not None, f"entity table {t!r} was not ensured"


# ---------------------------------------------------------------------------
# Lease-TTL threading - no live database (records the bound params)
# ---------------------------------------------------------------------------


class _RecordingConn:
    """Minimal asyncpg-conn stand-in that records fetch() calls."""

    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return []

    async def execute(self, query, *args):
        return "OK"


class _RecordingPool:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _RecordingSP:
    def __init__(self, conn: _RecordingConn) -> None:
        self.pool = _RecordingPool(conn)
        self.leases_table = '"primer"."leases"'
        self.schema = "primer"


@pytest.mark.asyncio
async def test_claim_due_binds_configured_lease_ttl():
    # A-I1: the configured lease TTL must be the $3 interval passed to the
    # claim query, not a hardcoded "60".
    conn = _RecordingConn()
    engine = PostgresClaimEngine(
        storage_provider=_RecordingSP(conn), adapters={}, lease_ttl_seconds=15,
    )
    await engine.claim_due("worker-A", max_count=5)
    assert conn.fetch_calls, "claim_due did not run a fetch"
    _query, args = conn.fetch_calls[-1]
    assert args == (5, "worker-A", "15")


@pytest.mark.asyncio
async def test_heartbeat_binds_configured_lease_ttl():
    conn = _RecordingConn()
    engine = PostgresClaimEngine(
        storage_provider=_RecordingSP(conn), adapters={}, lease_ttl_seconds=15,
    )
    await engine.heartbeat("worker-A", [(ClaimKind.HARNESS, "c1")])
    assert conn.fetch_calls, "heartbeat did not run a fetch"
    query, args = conn.fetch_calls[-1]
    # TTL threaded as the last ($4) param; no hardcoded 60s literal remains.
    assert args[-1] == "15"
    assert "'60 seconds'" not in query
    assert "$4" in query


@pytest.mark.asyncio
async def test_default_lease_ttl_is_60_seconds():
    conn = _RecordingConn()
    engine = PostgresClaimEngine(storage_provider=_RecordingSP(conn), adapters={})
    assert engine.lease_ttl_seconds == 60
    await engine.claim_due("worker-A", max_count=1)
    _query, args = conn.fetch_calls[-1]
    assert args[-1] == "60"


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_has_lease_counts_armed_claimed_and_expired_rows(
    pg_storage, entity_seeder,
):
    """has_lease is the row, has_live_lease is the live claim; they differ for an armed
    lease nobody has claimed and for a claim that has expired (the claim loop reclaims it)."""
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapter = _NoJoinAdapter()
    engine = PostgresClaimEngine(
        storage_provider=pg_storage, adapters={ClaimKind.HARNESS: adapter},
    )
    await entity_seeder.seed(adapter.entity_table, ["h-1"])

    assert await engine.has_lease(ClaimKind.HARNESS, "h-1") is False

    await engine.upsert(ClaimKind.HARNESS, "h-1")
    assert await engine.has_lease(ClaimKind.HARNESS, "h-1") is True
    assert await engine.has_live_lease(ClaimKind.HARNESS, "h-1") is False

    (lease,) = await engine.claim_due("worker-A", max_count=1)
    assert await engine.has_lease(ClaimKind.HARNESS, "h-1") is True
    assert await engine.has_live_lease(ClaimKind.HARNESS, "h-1") is True

    async with pg_storage.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {pg_storage.leases_table} SET expires_at = now() - interval '1 second' "
            f"WHERE kind = 'harness' AND entity_id = 'h-1'"
        )
    assert await engine.has_live_lease(ClaimKind.HARNESS, "h-1") is False
    assert await engine.has_lease(ClaimKind.HARNESS, "h-1") is True

    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert await engine.has_lease(ClaimKind.HARNESS, "h-1") is False
