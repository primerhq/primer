"""The GIN index on the derived ``access.members`` tokens (access control, plan Task 4).

The list queries filter on ``members CONTAINS <token>`` (Task 3's
``visible_predicate`` emits exactly this), so each of the five owned tables
carries a GIN index on ``data->'access'->'members'``. Two layers, the shape of
``test_postgres_hot_indexes.py``:

* Pure DDL-shape assertions (no database) -- the ``_HOT_FIELD_INDEXES``
  registry declares the index for every owned table and
  ``_hot_field_index_ddl`` renders it.
* A live ``pg_indexes`` / ``EXPLAIN`` check, gated on
  ``PRIMER_TEST_POSTGRES_URL`` (``tests/pg_gate.py``), proving the index is
  created on first handle use and the ``visible_predicate`` find (Task 3) is
  served by it.

SQLite gets no index (a documented scan; single-process installs).
"""

from __future__ import annotations

import json

import pytest

from primer.access.person import Person
from primer.access.policy import visible_predicate
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.storage import OffsetPage
from primer.storage._predicate import _PredicateTranslator
from primer.storage.postgres import _HOT_FIELD_INDEXES, _hot_field_index_ddl
from tests.pg_gate import needs_postgres

_OWNED_TABLES = ["agent", "graph", "toolset", "workspacetemplate", "channel", "collection"]
_GIN_EXPR = "USING GIN ((data->'access'->'members'))"


# ---------------------------------------------------------------------------
# Pure DDL-shape assertions (no DB needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", _OWNED_TABLES)
def test_registry_declares_the_access_members_gin_index(table):
    """Every owned table carries a plain (non-unique) GIN index on the tokens."""
    (suffix, unique, expr), = [e for e in _HOT_FIELD_INDEXES[table] if e[0] == "access_members"]
    assert suffix == "access_members"
    assert unique is False
    assert expr == _GIN_EXPR


@pytest.mark.parametrize("table", _OWNED_TABLES)
def test_access_members_ddl_renders_a_plain_gin_index(table):
    """The DDL is idempotent, plain (NOT concurrent), targets the table, and
    uses the GIN access method on the members array (``data->``, not ``data->>``)."""
    qualified = f'"public"."{table}"'
    (stmt,) = [s for s in _hot_field_index_ddl(table, qualified) if "access_members" in s]
    assert "CREATE INDEX IF NOT EXISTS" in stmt
    assert "UNIQUE" not in stmt
    assert "CONCURRENTLY" not in stmt
    assert f'"{table}_access_members"' in stmt
    assert qualified in stmt
    assert "USING GIN" in stmt
    assert "data->'access'->'members'" in stmt


# ---------------------------------------------------------------------------
# Live pg_indexes / EXPLAIN introspection (gated on a real Postgres URL)
# ---------------------------------------------------------------------------

pg = needs_postgres("the access-members index tests")

_BULK_ROWS = 999


async def _index_names(sp, table: str) -> set[str]:
    async with sp.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT indexname FROM pg_indexes
             WHERE schemaname = $1 AND tablename = $2
            """,
            sp.schema,
            table,
        )
    return {r["indexname"] for r in rows}


@pg
@pytest.mark.asyncio
async def test_access_members_index_created_and_serves_the_contains_predicate(postgres_storage_provider):
    """First handle use creates the GIN index; on a 1000-row table the
    ``members CONTAINS`` find (what the list endpoint's visible predicate
    compiles to) is answered by it, and the same predicate through the
    storage API returns the owned row."""
    sp = postgres_storage_provider
    agents = sp.get_storage(Agent)
    # First handle use runs _ensure_table -> creates table + GIN + hot indexes.
    owned = await agents.create(
        Agent(description="d", model=AgentModel(profile_id="p"), access=Access.owned_by("alice"))
    )
    # Fill the table: bulk rows carry no membership tokens (valid model JSON,
    # written directly so the 1000-row build stays a single statement).
    base = Agent(description="bulk", model=AgentModel(profile_id="p"))
    base_dump = base.model_dump(mode="json")
    async with sp.pool.acquire() as conn:
        await conn.executemany(
            f'INSERT INTO "{sp.schema}"."agent" (id, data) VALUES ($1, $2::jsonb)',
            [
                (f"agent-bulk-{i:04d}", json.dumps({**base_dump, "id": f"agent-bulk-{i:04d}"}))
                for i in range(_BULK_ROWS)
            ],
        )
        await conn.execute(f'ANALYZE "{sp.schema}"."agent"')

    assert "agent_access_members" in await _index_names(sp, "agent")

    # EXPLAIN the exact WHERE clause the list endpoint compiles for this
    # person (visible_predicate, translated the way the storage layer does).
    # enable_seqscan = off forces the only remaining access path, so the plan
    # must name the GIN index if it exists and is usable.
    person = Person("alice", "user")
    predicate = visible_predicate(person)
    assert predicate is not None
    where_sql, where_params = _PredicateTranslator(Agent).translate(predicate)
    async with sp.pool.acquire() as conn:
        await conn.execute("SET enable_seqscan = off")
        plan = await conn.fetch(
            f'EXPLAIN (FORMAT JSON) SELECT id FROM "{sp.schema}"."agent" WHERE {where_sql}',
            *where_params,
        )
        await conn.execute("RESET enable_seqscan")
    assert "agent_access_members" in str(plan[0][0])

    # The same predicate through the storage API returns exactly the owned row.
    page = await agents.find(predicate, OffsetPage(offset=0, length=10))
    assert [row.id for row in page.items] == [owned.id]
