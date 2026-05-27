"""Tests for PostgresClaimEngine — upsert + delete_lease + claim_due.

Live Postgres tests require MATRIX_TEST_POSTGRES_URL and are skipped otherwise.
Pure SQL-builder unit tests (``test_build_claim_query_*``) run without any
database and are never skipped.

The fixture sets up a fresh PostgresStorageProvider (which creates the
leases table), pre-seeds any entity rows needed for claim_due tests,
then cleans up on exit.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from matrix.int.claim import ClaimKind, ReleaseOutcome
from matrix.claim.adapters.chats import ChatClaimAdapter
from matrix.claim.adapters.sessions import SessionClaimAdapter
from matrix.claim.postgres import PostgresClaimEngine
from matrix.claim.sql import build_claim_query
from matrix.model.provider import PoolConfig, PostgresConfig
from matrix.storage.postgres import PostgresStorageProvider


_URL_ENV = "MATRIX_TEST_POSTGRES_URL"

POSTGRES_AVAILABLE = bool(os.environ.get(_URL_ENV))

# Convenience decorator applied to each test that needs a live database.
_needs_pg = pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason=f"set {_URL_ENV} to run Postgres claim-engine tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_url(url: str) -> PostgresConfig:
    from matrix.model.except_ import ConfigError

    p = urlparse(url)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_URL_ENV}")
    query = parse_qs(p.query)
    schema = query.get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=p.port or 5432,
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
    url = os.environ.get(_URL_ENV)
    if not url:
        pytest.skip(f"set {_URL_ENV} to run Postgres claim-engine tests")

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
        ClaimKind.CHAT: ChatClaimAdapter(chat_storage=None),
    }
    return PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)


# ---------------------------------------------------------------------------
# Tests — upsert
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_upsert_creates_row(pg_engine, pg_storage):
    await pg_engine.upsert(ClaimKind.CHAT, "c-1", priority=100)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'c-1'"
        )

    assert row is not None
    assert row["priority_score"] == 100
    assert row["claimed_by"] is None


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_upsert_updates_priority(pg_engine, pg_storage):
    await pg_engine.upsert(ClaimKind.CHAT, "c-1", priority=100)
    await pg_engine.upsert(ClaimKind.CHAT, "c-1", priority=50)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT priority_score FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'c-1'"
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
    await pg_engine.upsert(ClaimKind.CHAT, "c-del", priority=100)
    await pg_engine.delete_lease(ClaimKind.CHAT, "c-del")

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT 1 FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'c-del'"
        )

    assert row is None


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_delete_lease_noop_on_missing(pg_engine):
    # Must not raise.
    await pg_engine.delete_lease(ClaimKind.CHAT, "not-there")


# ---------------------------------------------------------------------------
# Tests — claim_due
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_claim_due_claims_unclaimed(pg_engine, pg_storage):
    """claim_due with no adapters produces no-op SQL; returns empty list."""
    bare_engine = PostgresClaimEngine(
        storage_provider=pg_storage,
        adapters={},
    )
    await bare_engine.upsert(ClaimKind.CHAT, "c-bare")
    leases = await bare_engine.claim_due("worker-A", max_count=5)
    # No adapters → no CTEs → no rows claimed.
    assert leases == []


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_claim_due_respects_max_count(pg_storage):
    """Seed multiple leases; claim_due should respect max_count.

    Uses a synthetic adapter whose eligibility SQL only touches the
    lease alias (no entity table JOIN needed) so we don't have to seed
    any entity rows.
    """
    from matrix.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.CHAT
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            # Always-true fragment referencing only the lease row alias.
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapters = {ClaimKind.CHAT: _NoJoinAdapter()}
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

    for i in range(5):
        await engine.upsert(ClaimKind.CHAT, f"c-{i}")

    leases = await engine.claim_due("worker-A", max_count=3)
    assert len(leases) == 3
    assert all(lse.claimed_by == "worker-A" for lse in leases)


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_claim_due_skips_already_claimed(pg_storage):
    """A lease already claimed (within TTL) should not be returned again."""
    from matrix.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.SESSION
        entity_table = "sessions"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapters = {ClaimKind.SESSION: _NoJoinAdapter()}
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

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
    assert "chat_cand" not in sql
    assert "session_cand" not in sql


def test_build_claim_query_single_adapter():
    adapters = {ClaimKind.CHAT: ChatClaimAdapter(chat_storage=None)}
    sql = build_claim_query(adapters, '"test"."leases"')

    assert "chat_cand" in sql
    assert "chats" in sql
    # Only one CTE — no union needed.
    assert "UNION ALL" not in sql


def test_build_claim_query_multiple_adapters():
    adapters = {
        ClaimKind.CHAT: ChatClaimAdapter(chat_storage=None),
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
    }
    sql = build_claim_query(adapters, '"test"."leases"')

    assert "chat_cand" in sql
    assert "session_cand" in sql
    assert "UNION ALL" in sql
    assert "RETURNING" in sql


def test_build_claim_query_schema_qualifies_entity_tables():
    """When schema is provided, entity table JOINs use schema-qualified names."""
    adapters = {ClaimKind.CHAT: ChatClaimAdapter(chat_storage=None)}
    sql = build_claim_query(adapters, '"myschema"."leases"', schema="myschema")

    assert '"myschema"."chats"' in sql
