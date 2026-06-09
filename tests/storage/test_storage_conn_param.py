"""PostgresStorage.get/update accept an optional ``conn`` kwarg.

When a caller supplies a connection, the storage handle reads/writes on
that connection (a caller-opened transaction) instead of acquiring a
second connection from the pool. This is the foundation for making
PostgresClaimEngine.release atomic: the claim adapters' on_release can
write the entity row on the SAME transaction connection the engine
opened.

These are pool-less unit tests: the fake provider's ``pool.acquire``
raises, so any code path that tries to acquire instead of using the
supplied conn fails loudly.
"""

from __future__ import annotations

import json

import pytest

from primer.model.agent import Agent, AgentModel
from primer.storage.postgres import PostgresStorage, _table_ensured


class _FakeRow(dict):
    """asyncpg.Record-like: indexable by column name."""


class _ExplodingPool:
    """A pool whose acquire() must never be reached in these tests."""

    def acquire(self):  # noqa: D401 - intentionally explosive
        raise AssertionError("pool.acquire() must not be called when conn is supplied")


class _FakeProvider:
    """Minimal stand-in for PostgresStorageProvider."""

    schema = "public"

    def __init__(self) -> None:
        self.pool = _ExplodingPool()


class _FakeConn:
    """Caller-supplied connection: records SQL + returns a canned row."""

    def __init__(self, row: object | None) -> None:
        self._row = row
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, sql: str, *args: object) -> object | None:
        self.calls.append((sql, args))
        return self._row


def _make_storage() -> PostgresStorage[Agent]:
    storage = PostgresStorage[Agent](provider=_FakeProvider(), model_class=Agent)
    # Mark the table as ensured so _ensure_table() short-circuits without
    # touching the (exploding) pool. The cache key mirrors the production
    # code: (id(provider), model_class).
    _table_ensured.add((id(storage._provider), Agent))
    return storage


def _agent_row(agent_id: str) -> _FakeRow:
    data = {
        "description": "test agent",
        "model": {"provider_id": "p1", "model_name": "m1"},
    }
    return _FakeRow(id=agent_id, data=json.dumps(data))


@pytest.mark.asyncio
async def test_get_uses_provided_conn_without_acquiring() -> None:
    storage = _make_storage()
    conn = _FakeConn(_agent_row("a1"))

    got = await storage.get("a1", conn=conn)

    assert got is not None
    assert got.id == "a1"
    assert got.model.provider_id == "p1"
    assert len(conn.calls) == 1
    assert conn.calls[0][1] == ("a1",)


@pytest.mark.asyncio
async def test_get_returns_none_on_missing_row_with_conn() -> None:
    storage = _make_storage()
    conn = _FakeConn(None)

    got = await storage.get("missing", conn=conn)

    assert got is None
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_update_uses_provided_conn_without_acquiring() -> None:
    storage = _make_storage()
    conn = _FakeConn(_agent_row("a1"))
    entity = Agent(
        id="a1",
        description="test agent",
        model=AgentModel(provider_id="p1", model_name="m1"),
    )

    got = await storage.update(entity, conn=conn)

    assert got is not None
    assert got.id == "a1"
    assert len(conn.calls) == 1
    # UPDATE binds (id, data_json) in that order.
    assert conn.calls[0][1][0] == "a1"


@pytest.mark.asyncio
async def test_acquire_or_use_yields_supplied_conn() -> None:
    storage = _make_storage()
    sentinel = object()
    async with storage._acquire_or_use(sentinel) as c:
        assert c is sentinel


@pytest.mark.asyncio
async def test_acquire_or_use_acquires_when_conn_is_none() -> None:
    # With conn=None the helper must fall through to pool.acquire(),
    # which our exploding pool rejects -- proving it takes that branch.
    storage = _make_storage()
    with pytest.raises(AssertionError, match="acquire"):
        async with storage._acquire_or_use(None):
            pass
