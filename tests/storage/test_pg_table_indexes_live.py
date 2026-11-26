"""Review lane 3 (3.4, 3.7, 3.10) against a real Postgres: how entity tables and the event schema are created.

Gated on ``PRIMER_TEST_POSTGRES_URL`` (``tests/pg_gate.py``); each test uses its own schema.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import pytest
import pytest_asyncio

from primer.model.api_token import ApiToken
from primer.model.artifact import ArtifactLocation
from primer.model.collection import Document
from primer.model.oidc import UserIdentity
from primer.model.trigger import Subscription
from primer.model.user import User
from primer.model.workspace_session import WorkspaceSession
from primer.storage import postgres as pg_mod
from primer.storage.postgres import _HOT_FIELD_INDEXES, PostgresStorageProvider, _table_name_for
from tests.pg_gate import postgres_marks, require_postgres_url
from tests.storage.test_system_state import _parse_postgres_url

pytestmark = postgres_marks("the table-index tests")

HOT = "apitoken_token_hash_uniq"
GIN = "apitoken_data_gin"


def _provider(schema: str) -> PostgresStorageProvider:
    cfg = _parse_postgres_url(require_postgres_url("the table-index tests")).model_copy(update={"db_schema": schema})
    return PostgresStorageProvider(cfg)


@pytest_asyncio.fixture
async def schema() -> AsyncIterator[str]:
    name = f"t{uuid.uuid4().hex[:16]}"
    yield name
    sp = _provider("public")
    await sp.initialize()
    try:
        async with sp.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    finally:
        await sp.aclose()


async def _indexes(sp: PostgresStorageProvider, table: str = "apitoken") -> set[str]:
    async with sp.pool.acquire() as conn:
        rows = await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = $2", sp.schema, table)
    return {r["indexname"] for r in rows}


def _conn_kwargs(sp: PostgresStorageProvider) -> dict:
    cfg = sp._config  # noqa: SLF001
    return dict(host=cfg.hostname, port=cfg.port, user=cfg.username, password=cfg.password.get_secret_value(), database=cfg.database)


async def _legacy_table(schema: str, model: type = ApiToken, *, drop: str = "all", gin: bool = True) -> str:
    """The table as an older release left it: hot indexes of kind ``drop`` ("all", "unique" or "nonunique") missing, the GIN present."""
    sp = _provider(schema)
    await sp.initialize()
    table = _table_name_for(model)
    try:
        await sp.get_storage(model)._ensure_table()
        async with sp.pool.acquire() as conn:
            for suffix, unique, _expr in _HOT_FIELD_INDEXES.get(table, ()):
                if drop == "all" or (drop == "unique") == unique:
                    await conn.execute(f'DROP INDEX "{schema}"."{table}_{suffix}"')
            if gin:
                await conn.execute(f'CREATE INDEX "{table}_data_gin" ON "{schema}"."{table}" USING gin (data jsonb_path_ops)')
    finally:
        await sp.aclose()
    return table


def _tok(i: int, h: str) -> ApiToken:
    return ApiToken(id=f"t{i}", user_id="u", name="n", token_hash=h, prefix="abcdefgh", created_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_new_tables_get_hot_indexes_and_no_unusable_gin(schema: str) -> None:
    """3.7: no whole-document GIN is created, and the hot index still is (inline, atomically with the table)."""
    sp = _provider(schema)
    await sp.initialize()
    try:
        await sp.get_storage(ApiToken)._ensure_table()
        names = await _indexes(sp)
        assert HOT in names
        assert GIN not in names
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_existing_table_is_usable_at_once_and_nonunique_indexes_are_fixed_in_the_background(schema: str) -> None:
    """3.4: an existing table is ensured without waiting for a missing non-unique index; the index appears. An existing GIN is left alone (N-3)."""
    table = await _legacy_table(schema, Subscription, drop="nonunique")
    sp = _provider(schema)
    await sp.initialize()
    try:
        handle = sp.get_storage(Subscription)
        await handle._ensure_table()
        assert handle._table_ensured
        assert handle._index_task is not None
        await asyncio.wait_for(handle._index_task, 30)
        names = await _indexes(sp, table)
        assert f"{table}_trigger_id" in names
        assert f"{table}_data_gin" in names  # N-3: not dropped
    finally:
        await sp.aclose()


async def _block_index_builds(sp: PostgresStorageProvider, schema: str, tables: list[str]) -> tuple[asyncpg.Connection, object]:
    """Hold a lock that conflicts with every index build (plain and CONCURRENTLY) but not with reads."""
    blocker = await asyncpg.connect(**_conn_kwargs(sp))
    tx = blocker.transaction()
    await tx.start()
    for table in tables:
        await blocker.execute(f'LOCK TABLE "{schema}"."{table}" IN SHARE ROW EXCLUSIVE MODE')
    return blocker, tx


@pytest.mark.asyncio
async def test_a_stuck_index_build_does_not_break_the_table_or_retry_per_request(schema: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """3.4: a build that cannot finish leaves the table fully usable, and later requests do not start another build."""
    table = await _legacy_table(schema, Subscription, drop="nonunique")
    monkeypatch.setattr(pg_mod, "_INDEX_BUILD_TIMEOUT", 0.5)
    monkeypatch.setattr(pg_mod, "_INDEX_RETRY_BACKOFF", (3600.0,))
    sp = _provider(schema)
    await sp.initialize()
    blocker, tx = await _block_index_builds(sp, schema, [table])
    try:
        handle = sp.get_storage(Subscription)
        builds = 0
        real = handle._build_missing_indexes

        async def counted(*a, **k) -> None:
            nonlocal builds
            builds += 1
            await real(*a, **k)

        handle._build_missing_indexes = counted  # type: ignore[method-assign]
        for _ in range(5):  # five "requests"
            await handle._ensure_table()
        assert handle._table_ensured
        assert await handle.get("nope") is None  # the table answers while the build is stuck
        await asyncio.sleep(1.5)  # past the build timeout: the build has failed once
        for _ in range(5):
            await handle._ensure_table()
        assert builds == 1  # one background attempt (now sleeping out its backoff), not one per request
        assert f"{table}_trigger_id" not in await _indexes(sp, table)
    finally:
        await tx.rollback()
        await blocker.close()
        await sp.aclose()  # cancels the sleeping background task


@pytest.mark.asyncio
async def test_a_unique_index_is_built_before_the_table_takes_writes(schema: str) -> None:
    """N-1: an existing table missing a UNIQUE hot index gets it before the first write, so two concurrent creates of one token hash leave one row."""
    await _legacy_table(schema, ApiToken, drop="unique")
    sp = _provider(schema)
    await sp.initialize()
    try:
        handle = sp.get_storage(ApiToken)
        dup = "a" * 64
        results = await asyncio.gather(handle.create(_tok(1, dup)), handle.create(_tok(2, dup)), return_exceptions=True)
        assert sum(isinstance(r, BaseException) for r in results) == 1
        async with sp.pool.acquire() as c:
            n = await c.fetchval(f"SELECT count(*) FROM \"{schema}\".apitoken WHERE data->>'token_hash' = $1", dup)
        assert n == 1
        assert HOT in await _indexes(sp)
    finally:
        await sp.aclose()


async def _duplicate_rows(schema: str, model: type, make_row: object, dropped: str) -> str:
    """A legacy table that already holds two rows with the same unique value and lacks the unique index ``dropped``."""
    sp = _provider(schema)
    await sp.initialize()
    table = _table_name_for(model)
    try:
        handle = sp.get_storage(model)
        await handle.create(make_row("r1"))  # type: ignore[operator]
        async with sp.pool.acquire() as c:
            await c.execute(f'DROP INDEX "{schema}"."{table}_{dropped}"')
            await c.execute(f'INSERT INTO "{schema}"."{table}" (id, data) SELECT \'copy\', data FROM "{schema}"."{table}" WHERE id = \'r1\'')
    finally:
        await sp.aclose()
    return table


@pytest.mark.asyncio
async def test_a_duplicate_blocking_a_tolerant_unique_index_is_logged_truthfully(schema: str, caplog: pytest.LogCaptureFixture) -> None:
    """N-1/m3: on duplicate rows the index is absent, the warning names the index and says duplicates exist, and the table still works."""
    table = await _duplicate_rows(schema, User, lambda i: User(id=i, username="dup", password_hash=None, created_at=datetime.now(UTC)), "username_uniq")
    caplog.set_level(logging.WARNING)
    sp = _provider(schema)
    await sp.initialize()
    try:
        handle = sp.get_storage(User)
        await handle._ensure_table()
        assert handle._table_ensured
        assert f"{table}_username_uniq" not in await _indexes(sp, table)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and f"{table}_username_uniq" in r.getMessage()]
        assert warnings and "duplicate" in warnings[0]
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_a_duplicate_blocking_a_strict_unique_index_is_an_error_not_a_silent_skip(schema: str) -> None:
    """N-1: a unique index that is not duplicate-tolerant and cannot be built is a ProviderError, not silently absent."""
    from primer.model.except_ import ProviderError

    await _duplicate_rows(schema, ApiToken, lambda i: _tok(1, "b" * 64).model_copy(update={"id": i}), "token_hash_uniq")
    sp = _provider(schema)
    await sp.initialize()
    try:
        with pytest.raises(ProviderError):
            await sp.get_storage(ApiToken)._ensure_table()
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_concurrent_first_use_starts_one_background_task_and_aclose_is_prompt(schema: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """N-2: three concurrent first uses start at most one task for the handle, and aclose returns promptly with a build in flight."""
    table = await _legacy_table(schema, Subscription, drop="nonunique")
    sp = _provider(schema)
    await sp.initialize()
    blocker, tx = await _block_index_builds(sp, schema, [table])
    try:
        handle = sp.get_storage(Subscription)
        started = 0
        real = handle._maintain_indexes

        async def counted(*a, **k) -> None:
            nonlocal started
            started += 1
            await real(*a, **k)

        handle._maintain_indexes = counted  # type: ignore[method-assign]
        await asyncio.gather(handle._ensure_table(), handle._ensure_table(), handle._ensure_table())
        await asyncio.sleep(0.5)  # the build is now waiting on the blocker's lock
        assert started == 1
        await asyncio.wait_for(sp.aclose(), 5)
    finally:
        await tx.rollback()
        await blocker.close()


@pytest.mark.asyncio
async def test_no_background_task_when_nothing_is_missing(schema: str) -> None:
    """N-2: a table whose indexes are all present starts nothing."""
    await _legacy_table(schema, Subscription, drop="unique")  # Subscription has no unique hot index: nothing dropped
    sp = _provider(schema)
    await sp.initialize()
    try:
        handle = sp.get_storage(Subscription)
        await handle._ensure_table()
        assert handle._index_task is None
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_an_invalid_index_is_rebuilt_not_counted_as_present(schema: str) -> None:
    """m1: an INVALID index left by a failed CONCURRENTLY build is dropped and rebuilt."""
    table = await _legacy_table(schema, Subscription, drop="nonunique", gin=False)
    sp = _provider(schema)
    await sp.initialize()
    name = f"{table}_trigger_id"
    try:
        async with sp.pool.acquire() as c:
            await c.execute(f'CREATE INDEX "{name}" ON "{schema}"."{table}" ((data->>\'trigger_id\'))')
            await c.execute(f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '\"{schema}\".\"{name}\"'::regclass")
        handle = sp.get_storage(Subscription)
        await handle._ensure_table()
        assert handle._index_task is not None
        await asyncio.wait_for(handle._index_task, 30)
        async with sp.pool.acquire() as c:
            valid = await c.fetchval(f"SELECT indisvalid FROM pg_index WHERE indexrelid = '\"{schema}\".\"{name}\"'::regclass")
        assert valid is True
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_background_builds_of_many_tables_are_bounded_per_provider(schema: str) -> None:
    """m2: an upgrade boot touching many tables never has more than two background builds in flight."""
    models = [Subscription, UserIdentity, ArtifactLocation, WorkspaceSession]
    tables = [await _legacy_table(schema, m, drop="nonunique", gin=False) for m in models]
    sp = _provider(schema)
    await sp.initialize()
    blocker, tx = await _block_index_builds(sp, schema, tables)
    try:
        for m in models:
            await sp.get_storage(m)._ensure_table()
        await asyncio.sleep(1.0)
        async with sp.pool.acquire() as c:
            running = await c.fetchval("SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'CREATE INDEX CONCURRENTLY%' AND pid <> pg_backend_pid()")
        assert 1 <= running <= 2
    finally:
        await asyncio.wait_for(sp.aclose(), 5)
        await tx.rollback()
        await blocker.close()


def _conn_kwargs(sp: PostgresStorageProvider) -> dict:
    cfg = sp._config  # noqa: SLF001
    return dict(host=cfg.hostname, port=cfg.port, user=cfg.username, password=cfg.password.get_secret_value(), database=cfg.database)


@pytest.mark.asyncio
async def test_event_schema_boot_survives_a_concurrent_create(schema: str) -> None:
    """3.10: the loser of a concurrent events-schema create treats it as success instead of crashing the lifespan."""
    sp = _provider(schema)
    await sp.initialize()
    try:
        store = sp.get_event_store()
        real_pool = store._pool  # noqa: SLF001

        class _Conn:
            def transaction(self):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, *_a, **_k):
                raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint \"pg_type_typname_nsp_index\"")

        class _Pool:
            def acquire(self):
                return _Conn()

        store._pool = _Pool()  # noqa: SLF001
        await store.ensure_schema()  # must not raise
        store._pool = real_pool  # noqa: SLF001
        await store.ensure_schema()  # and the real path still works
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_concurrent_boots_on_a_fresh_schema_all_succeed(schema: str) -> None:
    """3.10 smoke: several processes' worth of providers boot the events schema at once."""
    providers = [_provider(schema) for _ in range(6)]
    for p in providers:
        await p.initialize()
    try:
        await asyncio.gather(*(p.get_event_store().ensure_schema() for p in providers))
    finally:
        for p in providers:
            await p.aclose()


def _small_pool_provider(schema: str, *, max_size: int, acquire_timeout: float = 30.0) -> PostgresStorageProvider:
    cfg = _parse_postgres_url(require_postgres_url("the table-index tests"))
    cfg = cfg.model_copy(update={"db_schema": schema, "pool": cfg.pool.model_copy(update={"min_size": 1, "max_size": max_size, "acquire_timeout": acquire_timeout})})
    return PostgresStorageProvider(cfg)


async def _make_table(schema: str, model: type) -> None:
    sp = _provider(schema)
    await sp.initialize()
    try:
        await sp.get_storage(model)._ensure_table()
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_concurrent_transactional_first_uses_complete_on_a_small_pool(schema: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """N2-1: three staggered transactional first uses on a 3-connection pool all complete: with the snapshot showing nothing missing, the ensure pass takes no extra pool connection under its lock (the peers hold the other connections in their transactions)."""
    from primer.model.except_ import ProviderError

    await _make_table(schema, ApiToken)  # the common upgrade case: table exists, every index present
    real_state = pg_mod.PostgresStorage._index_state
    calls = 0

    async def slow_state(self, conn):  # widen the first holder's DDL window so the peers queue deterministically
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.3)
        return await real_state(self, conn)

    monkeypatch.setattr(pg_mod.PostgresStorage, "_index_state", slow_state)
    sp = _small_pool_provider(schema, max_size=3)
    await sp.initialize()
    handle = sp.get_storage(ApiToken)
    try:

        async def reader(delay: float) -> None:
            await asyncio.sleep(delay)
            async with sp.transaction() as conn:
                await handle.get("nope", conn=conn)

        tasks = [asyncio.create_task(reader(d)) for d in (0.0, 0.1, 0.2)]
        done, pending = await asyncio.wait(tasks, timeout=15)
        assert not pending, f"{len(pending)}/3 first uses still waiting: the ensure pass blocked on a pool connection under its lock"
        for t in done:
            t.result()  # surface any ProviderError rather than swallow it
    finally:
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(sp.aclose(), 5)


@pytest.mark.asyncio
async def test_ensure_raises_within_the_acquire_timeout_on_a_saturated_pool(schema: str) -> None:
    """N2-1: the only pool connection (max_size=1) held in an open transaction saturates the pool; the ensure pass raises a ProviderError naming the table within the acquire timeout instead of hanging."""
    from primer.model.except_ import ProviderError

    table = await _legacy_table(schema, ApiToken, drop="unique")  # a genuinely missing unique index
    sp = _small_pool_provider(schema, max_size=1, acquire_timeout=2.0)
    await sp.initialize()
    handle = sp.get_storage(ApiToken)
    try:
        async with sp.transaction() as conn:  # the only pool connection, inside a transaction
            started = time.monotonic()
            with pytest.raises(ProviderError, match=f"timed out.*{table}"):
                await asyncio.wait_for(handle.get("nope", conn=conn), timeout=15)  # a regression to the unbounded wait fails here, fast
        assert time.monotonic() - started < 10  # it raised on ITS OWN acquire timeout, not on this guard
    finally:
        await asyncio.wait_for(sp.aclose(), 5)


@pytest.mark.asyncio
async def test_an_existing_document_table_gets_the_children_index_in_the_background(schema: str) -> None:
    """M1 3.5: a document table older than the (collection_id, parent_id) children index gets it from the background CONCURRENTLY path; the request path does not wait for it."""
    await _make_table(schema, Document)
    sp = _provider(schema)
    await sp.initialize()
    try:
        async with sp.pool.acquire() as conn:
            await conn.execute(f'DROP INDEX "{schema}"."document_parent_id"')
        await sp.get_storage(Document).get("nope")  # the table is already ensured by this provider
    finally:
        await sp.aclose()
    sp = _provider(schema)  # a fresh process: first use finds the non-unique index missing
    await sp.initialize()
    try:
        handle = sp.get_storage(Document)
        await handle.get("nope")  # usable at once; the build runs in the background
        for _ in range(50):
            if "document_parent_id" in await _indexes(sp, "document"):
                break
            await asyncio.sleep(0.2)
        assert "document_parent_id" in await _indexes(sp, "document")
    finally:
        await sp.aclose()
