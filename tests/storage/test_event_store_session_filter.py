"""The event store's ``session_id`` filter on ``read_after`` (review 2026-11-23 X2.2, the L 2.8 follow-up).

The reflection lookup scan is bounded by the session's own events (the ``(session_id, id)`` index), not by wall-clock
time: every store applies the filter in SQL (or its in-memory equivalent), conjunctively with the other ``read_after``
filters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.events.memory_store import InMemoryEventStore
from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig, SqliteConfig
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import CANONICAL_ENV, explicit_port, needs_postgres, require_postgres_url

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

pg = needs_postgres("event store session filter tests")


async def _seed(store) -> None:
    for i, sid in enumerate(("s1", "s2", "s1", "s2", "s1")):
        await store.append(
            event_type="memory.lookup", actor="memory:a1", entity_kind="agent", entity_id="a1",
            session_id=sid, payload={"turn": i + 1}, occurred_at=NOW + timedelta(seconds=i),
        )


async def _assert(store) -> None:
    page = await store.read_after(
        0, event_type_prefix="memory.lookup", entity_kind="agent", entity_id="a1", session_id="s1",
    )
    assert [e.session_id for e in page] == ["s1", "s1", "s1"]
    assert [e.payload["turn"] for e in page] == [1, 3, 5]
    other = await store.read_after(0, session_id="s2")
    assert [e.payload["turn"] for e in other] == [2, 4]
    # The filter is conjunctive: a mismatch on any other filter returns nothing.
    assert await store.read_after(0, session_id="s1", entity_id="someone-else") == []
    assert await store.read_after(0, event_type_prefix="memory.reflection", session_id="s1") == []


@pytest.mark.asyncio
async def test_sqlite_read_after_filters_session_id(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        await _seed(provider.get_event_store())
        await _assert(provider.get_event_store())
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_in_memory_read_after_filters_session_id():
    store = InMemoryEventStore()
    await _seed(store)
    await _assert(store)


def _parse_url(url: str) -> PostgresConfig:
    p = urlparse(url)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {CANONICAL_ENV}")
    query = parse_qs(p.query)
    schema = query.get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=explicit_port(p),
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=2),
    )


@pytest_asyncio.fixture
async def postgres_provider():
    """A Postgres storage provider against the gate URL; the events table is emptied around each test."""
    url = require_postgres_url("event store session filter tests")
    sp = PostgresStorageProvider(_parse_url(url))
    await sp.initialize()
    store = sp.get_event_store()
    await store.ensure_schema()
    async with sp.pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {store._qualified}")
    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {store._qualified}")
        await sp.aclose()


@pg
@pytest.mark.asyncio
async def test_postgres_read_after_filters_session_id(postgres_provider):
    store = postgres_provider.get_event_store()
    await _seed(store)
    await _assert(store)
