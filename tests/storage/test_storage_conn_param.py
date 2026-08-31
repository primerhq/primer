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

    async def _ensure_events_schema(self) -> None:
        # Registered kinds (Agent is one whenever the routers were
        # imported in this process) emit CRUD events on the caller's
        # conn; the schema ensure is a no-op here.
        return


class _FakeConn:
    """Caller-supplied connection: records SQL + returns a canned row.

    Carries an asyncpg-shaped ``transaction()`` because registered
    kinds wrap the entity write + event append in one; ``execute``
    records the event INSERT the seam issues on the same conn.
    """

    def __init__(self, row: object | None) -> None:
        self._row = row
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, sql: str, *args: object) -> object | None:
        self.calls.append((sql, args))
        return self._row

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def _txn():
            yield

        return _txn()


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
        "model": {"profile_id": "p1--m1"},
    }
    return _FakeRow(id=agent_id, data=json.dumps(data))


@pytest.mark.asyncio
async def test_get_uses_provided_conn_without_acquiring() -> None:
    storage = _make_storage()
    conn = _FakeConn(_agent_row("a1"))

    got = await storage.get("a1", conn=conn)

    assert got is not None
    assert got.id == "a1"
    assert got.model.profile_id == "p1--m1"
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
        model=AgentModel(profile_id="p1--m1"),
    )

    got = await storage.update(entity, conn=conn)

    assert got is not None
    assert got.id == "a1"
    # First statement is the UPDATE, binding (id, data_json) in that
    # order. When the Agent kind is registered (routers imported in
    # this process) the emission seam adds one event INSERT on the
    # SAME caller-supplied conn - never a pooled one.
    assert conn.calls[0][1][0] == "a1"
    assert len(conn.calls) in (1, 2)
    if len(conn.calls) == 2:
        assert "events" in conn.calls[1][0]


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
