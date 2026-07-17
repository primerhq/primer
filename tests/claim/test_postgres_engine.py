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

import os
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.claim.adapters.chats import ChatClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.postgres import PostgresClaimEngine
from primer.claim.sql import build_claim_query
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider


_URL_ENV = "PRIMER_TEST_POSTGRES_URL"

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
    from primer.model.except_ import ConfigError

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
    from primer.int.claim import ClaimAdapter

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
    from primer.int.claim import ClaimAdapter

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
    assert "chat" in sql
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

    assert '"myschema"."chat"' in sql


# ---------------------------------------------------------------------------
# Tests — heartbeat
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_heartbeat_refreshes_expiry(pg_engine, pg_storage):
    """heartbeat extends expires_at and confirms the (kind, entity_id) pair."""
    from datetime import UTC, timedelta

    # Use the no-join adapter trick so no entity row is needed.
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.CHAT
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapters = {ClaimKind.CHAT: _NoJoinAdapter()}
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

    await engine.upsert(ClaimKind.CHAT, "hb-1")
    [lease] = await engine.claim_due("worker-A", max_count=1)

    # Record the expires_at BEFORE heartbeat.
    async with pg_storage.pool.acquire() as conn:
        before_row = await conn.fetchrow(
            f"SELECT expires_at FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'hb-1'"
        )
    import asyncio
    await asyncio.sleep(0.05)

    confirmed = await engine.heartbeat("worker-A", [(ClaimKind.CHAT, "hb-1")])
    assert confirmed == [(ClaimKind.CHAT, "hb-1")]

    async with pg_storage.pool.acquire() as conn:
        after_row = await conn.fetchrow(
            f"SELECT expires_at FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'hb-1'"
        )
    assert after_row["expires_at"] >= before_row["expires_at"]


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_heartbeat_rejects_non_owner(pg_engine, pg_storage):
    """heartbeat with the wrong worker_id returns an empty list."""
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.SESSION
        entity_table = "sessions"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapters = {ClaimKind.SESSION: _NoJoinAdapter()}
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

    await engine.upsert(ClaimKind.SESSION, "hb-wrong")
    await engine.claim_due("worker-A", max_count=1)

    confirmed = await engine.heartbeat("worker-B", [(ClaimKind.SESSION, "hb-wrong")])
    assert confirmed == []


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
async def test_postgres_release_drop_lease_deletes_row(pg_storage):
    """release with drop_lease=True removes the lease row."""
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.CHAT
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapters = {ClaimKind.CHAT: _NoJoinAdapter()}
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

    await engine.upsert(ClaimKind.CHAT, "rel-drop")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT 1 FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'rel-drop'"
        )
    assert row is None


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_release_without_drop_clears_claim_fields(pg_storage):
    """release without drop_lease clears claimed_by and makes row reclaimable."""
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.CHAT
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapters = {ClaimKind.CHAT: _NoJoinAdapter()}
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

    await engine.upsert(ClaimKind.CHAT, "rel-clear")
    [lease] = await engine.claim_due("worker-A", max_count=1)
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=False))

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT claimed_by, attempt_count FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'rel-clear'"
        )
    assert row is not None
    assert row["claimed_by"] is None
    assert row["attempt_count"] == 0


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_release_failure_bumps_attempt_count(pg_storage):
    """release with success=False increments attempt_count and stores last_error."""
    from datetime import timedelta
    from primer.int.claim import ClaimAdapter

    class _NoJoinAdapter(ClaimAdapter):
        kind = ClaimKind.CHAT
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "l.kind IS NOT NULL"

        async def on_release(self, conn, entity_id, *, outcome): ...

    adapters = {ClaimKind.CHAT: _NoJoinAdapter()}
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

    await engine.upsert(ClaimKind.CHAT, "rel-fail")
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
            f"WHERE kind = 'chat' AND entity_id = 'rel-fail'"
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

    Scenario: create a Chat entity with turn_status='claimable', upsert +
    claim its lease, release with drop_lease=True, then verify the lease
    row is gone AND the chat's turn_status became 'idle'.
    """
    from datetime import datetime, UTC
    from primer.model.chats import Chat
    from primer.storage.postgres import PostgresStorage

    # Prepare a real chat storage backed by the test provider.
    from primer.int.storage import Storage
    chat_storage: Storage[Chat] = pg_storage.get_storage(Chat)

    # Create a chat row with turn_status='claimable'.
    chat = Chat(
        id="txn-chat-1",
        agent_id="agent-x",
        created_at=datetime.now(UTC),
        status="active",
        turn_status="claimable",
    )
    await chat_storage.create(chat)

    try:
        # Wire up the real ChatClaimAdapter with actual storage.
        adapter = ChatClaimAdapter(chat_storage=chat_storage)

        class _EligibleAdapter(type(adapter)):
            """Override eligibility so no extra entity state is required."""
            def eligibility_sql(self) -> str:
                return "l.kind IS NOT NULL"

        real_adapter = _EligibleAdapter(chat_storage=chat_storage)
        adapters = {ClaimKind.CHAT: real_adapter}
        engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

        await engine.upsert(ClaimKind.CHAT, chat.id)
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
                f"WHERE kind = 'chat' AND entity_id = $1",
                chat.id,
            )
        assert lease_row is None, "Lease row should have been deleted"

        # Verify chat.turn_status is now 'idle'.
        updated_chat = await chat_storage.get(chat.id)
        assert updated_chat is not None
        assert updated_chat.turn_status == "idle", (
            f"Expected turn_status='idle', got {updated_chat.turn_status!r}"
        )

    finally:
        # Clean up the chat entity row.
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
    from primer.model.chats import Chat

    chat_storage: Storage[Chat] = pg_storage.get_storage(Chat)
    chat = Chat(
        id="fenced-chat-1",
        agent_id="agent-x",
        created_at=datetime.now(UTC),
        status="active",
        turn_status="claimable",
    )
    await chat_storage.create(chat)

    try:
        adapter = ChatClaimAdapter(chat_storage=chat_storage)

        class _EligibleAdapter(type(adapter)):
            """Override eligibility so no extra entity state is required."""
            def eligibility_sql(self) -> str:
                return "l.kind IS NOT NULL"

        real_adapter = _EligibleAdapter(chat_storage=chat_storage)
        adapters = {ClaimKind.CHAT: real_adapter}
        engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

        await engine.upsert(ClaimKind.CHAT, chat.id)
        [lease_a] = await engine.claim_due("worker-A", max_count=1)
        assert lease_a.claimed_by == "worker-A"

        # Simulate worker B re-claiming the lease: flip claimed_by to B
        # directly in the leases table (as a real second worker's
        # claim_due would have done).
        async with pg_storage.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {pg_storage.leases_table} "
                f"   SET claimed_by = 'worker-B' "
                f" WHERE kind = 'chat' AND entity_id = $1",
                chat.id,
            )

        # A's release must no-op (fence mismatch). Use drop_lease=True +
        # success=True so, absent the fence, it would delete the lease row
        # AND on_release would flip turn_status to 'idle'.
        with caplog.at_level(logging.WARNING, logger="primer.claim.postgres"):
            await engine.release(
                lease_a,
                outcome=ReleaseOutcome(success=True, drop_lease=True),
            )

        # Lease row is UNCHANGED: still present and still owned by B.
        async with pg_storage.pool.acquire() as conn:
            lease_row = await conn.fetchrow(
                f"SELECT claimed_by FROM {pg_storage.leases_table} "
                f"WHERE kind = 'chat' AND entity_id = $1",
                chat.id,
            )
        assert lease_row is not None, "A's release should not delete B's lease"
        assert lease_row["claimed_by"] == "worker-B"

        # Entity row is UNCHANGED: on_release was skipped, so turn_status
        # is still 'claimable' (not 'idle').
        unchanged_chat = await chat_storage.get(chat.id)
        assert unchanged_chat is not None
        assert unchanged_chat.turn_status == "claimable"

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
    await pg_engine.mark_resumable(ClaimKind.CHAT, "mr-new", priority=30)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT priority_score, claimed_by FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'mr-new'"
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
    await pg_engine.upsert(ClaimKind.CHAT, "mr-exist", priority=100, next_attempt_at=future)

    # mark_resumable should bump it to priority=25 and reset next_attempt_at to now.
    await pg_engine.mark_resumable(ClaimKind.CHAT, "mr-exist", priority=25)

    async with pg_storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT priority_score, next_attempt_at FROM {pg_storage.leases_table} "
            f"WHERE kind = 'chat' AND entity_id = 'mr-exist'"
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

    await pg_engine.mark_resumable(ClaimKind.CHAT, "wr-mr-1", priority=40)
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result == (ClaimKind.CHAT, "wr-mr-1")

    await gen.aclose()


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
        for t in ("chat", "harness", "trigger", "sessions"):
            await conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{t}" CASCADE')

    adapters = {
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
        ClaimKind.CHAT: ChatClaimAdapter(chat_storage=None),
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
        ClaimKind.TRIGGER: TriggerClaimAdapter(storage=None),
    }
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters=adapters)

    # Must NOT raise UndefinedTableError even though no entity exists yet.
    leases = await engine.claim_due("w-fresh", max_count=5)
    assert leases == []

    # The engine created each entity table with the storage-convention name.
    async with pg_storage.pool.acquire() as conn:
        for t in ("chat", "harness", "trigger", "sessions"):
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
    await engine.heartbeat("worker-A", [(ClaimKind.CHAT, "c1")])
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
