"""CI e2e: `GET /v1/workspaces` answered 500 on Postgres because `PostgresStorage.count_by` used a name the module never imported.

The SQLite tests could not see it (a different class), so this drives the Postgres handle through a recording fake of asyncpg that behaves like
the server where it matters here: a statement that changes the schema, or a SET, inside a READ ONLY transaction is refused.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from primer.model.collection import Collection
from primer.model.storage import OffsetPage
from primer.model.workspace_session import WorkspaceSession
from primer.storage.postgres import PostgresStorage, _table_name_for
from tests.pg_gate import needs_postgres


class _ReadOnlyViolation(Exception):
    pass


class _Conn:
    def __init__(self, fetch_rows=None, count=0):
        self.statements: list[str] = []
        self.read_only_depth = 0
        self.isolation: list[tuple[str | None, bool]] = []
        self.fetch_rows, self.count = fetch_rows or [], count

    def _guard(self, sql: str) -> None:
        head = sql.lstrip().split(None, 1)[0].upper()
        if self.read_only_depth and head in {"CREATE", "ALTER", "DROP", "SET", "INSERT", "UPDATE", "DELETE"}:
            raise _ReadOnlyViolation(f"cannot execute {head} in a read-only transaction")

    async def fetch(self, sql, *args):
        self._guard(sql)
        self.statements.append(sql)
        return self.fetch_rows

    async def fetchval(self, sql, *args):
        self._guard(sql)
        self.statements.append(sql)
        return self.count

    async def execute(self, sql, *args):
        self._guard(sql)
        self.statements.append(sql)

    @asynccontextmanager
    async def transaction(self, isolation=None, readonly=False, **kw):
        self.isolation.append((isolation, readonly))
        self.read_only_depth += 1 if readonly else 0
        try:
            yield
        finally:
            self.read_only_depth -= 1 if readonly else 0


def _handle(model, conn):
    @asynccontextmanager
    async def acquire():
        yield conn

    handle = PostgresStorage.__new__(PostgresStorage)
    handle._provider = SimpleNamespace(pool=SimpleNamespace(acquire=acquire), schema="s")
    handle._model, handle._table = model, _table_name_for(model)
    handle._qualified = f'"s"."{handle._table}"'
    handle._table_ensured = True
    return handle


async def test_count_by_groups_the_sessions_of_the_given_workspaces():
    conn = _Conn(fetch_rows=[{"k": "w1", "n": 3}, {"k": "w2", "n": 1}, {"k": None, "n": 9}])
    counts = await _handle(WorkspaceSession, conn).count_by("workspace_id", ["w1", "w2", "w3"])
    assert counts == {"w1": 3, "w2": 1}
    (sql,) = conn.statements
    assert "GROUP BY data->>'workspace_id'" in sql and 'FROM "s"."sessions"' in sql


async def test_count_by_with_no_values_does_not_query():
    conn = _Conn()
    assert await _handle(WorkspaceSession, conn).count_by("workspace_id", []) == {}
    assert conn.statements == []


async def test_a_page_and_its_count_run_in_one_read_only_snapshot_with_no_ddl_inside():
    conn = _Conn(fetch_rows=[], count=450)
    page = await _handle(Collection, conn).list(OffsetPage(offset=900, length=200))
    assert page.total == 450 and conn.isolation == [("repeatable_read", True)]
    assert all(not s.lstrip().upper().startswith(("CREATE", "ALTER", "SET")) for s in conn.statements)


async def test_the_spy_itself_refuses_ddl_in_a_read_only_transaction():
    conn = _Conn()
    async with conn.transaction(readonly=True):
        with pytest.raises(_ReadOnlyViolation):
            await conn.execute("CREATE INDEX x ON t (a)")


pg = needs_postgres("Postgres count_by and page tests")


@pg
async def test_workspace_list_shape_on_a_real_postgres(postgres_storage_provider):
    """The route's two storage calls with sessions present: a page of workspaces and the grouped session counts."""
    from datetime import datetime, timezone

    from primer.model.workspace_session import AgentSessionBinding, SessionStatus

    import uuid

    from primer.model.storage import FieldRef, Op, Predicate, Value

    sessions = postgres_storage_provider.get_storage(WorkspaceSession)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # the lane's database is shared across tests, so this test scopes every read to its own workspaces
    tag = uuid.uuid4().hex[:8]
    w1, w2 = f"cb-{tag}-w1", f"cb-{tag}-w2"
    for ws, n in {w1: 3, w2: 1}.items():
        for i in range(n):
            await sessions.create(WorkspaceSession(
                id=f"{ws}-{i}", workspace_id=ws, binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING, created_at=now,
            ))
    assert await sessions.count_by("workspace_id", [w1, w2, f"cb-{tag}-none"]) == {w1: 3, w2: 1}
    mine = Predicate(left=FieldRef(name="workspace_id"), op=Op.IN, right=Value(value=[w1, w2]))
    page = await sessions.find(mine, OffsetPage(offset=0, length=2))
    assert page.length == 2 and page.total == 4
    short = await sessions.find(mine, OffsetPage(offset=2, length=10, want_total=False))
    assert short.length == 2 and short.total == 4
