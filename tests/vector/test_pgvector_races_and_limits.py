"""T126 (GI-22, GI-27, GI-29): a concurrent create reuses the winner's collection, a search asks for the candidate list it needs, and the HNSW settings
pgvector refuses are refused at the save door."""

from __future__ import annotations

from contextlib import asynccontextmanager

import asyncpg
import pytest

from primer.model.except_ import ConflictError, ProviderError
from primer.model.providers.vector import PgVectorConfig, hnsw_bound_violation
from primer.vector.pgvector import PgVectorStoreProvider, _CollectionMeta

pytestmark = pytest.mark.asyncio


def _cfg(**kw) -> PgVectorConfig:
    return PgVectorConfig(hostname="h", username="u", password="p", database="d", **kw)


class _Conn:
    """A fake connection: the catalogue read answers ``rows`` in turn; the catalogue INSERT raises ``insert_error``."""

    def __init__(self, rows=(None,), insert_error=None, ddl_error=None):
        self.rows, self.insert_error, self.ddl_error = list(rows), insert_error, ddl_error
        self.executed: list[str] = []
        self.fetched: list[tuple] = []
        self.transactions = 0

    async def fetchrow(self, sql, *args):
        self.executed.append(sql)
        return self.rows.pop(0) if len(self.rows) > 1 else self.rows[0]

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if self.ddl_error is not None and sql.startswith("CREATE TABLE"):
            raise self.ddl_error
        if self.insert_error is not None and "INSERT INTO" in sql and "primer_collections" in sql:
            raise self.insert_error

    async def fetch(self, sql, *args):
        self.fetched.append((sql, args))
        return []

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            self.transactions += 1
            yield
        return _tx()


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _acq():
            yield self.conn
        return _acq()


def _store(conn, **cfg):
    provider = PgVectorStoreProvider(_cfg(**cfg))
    provider._pool = _Pool(conn)  # noqa: SLF001
    return provider.get_vector_store()


def _winner(dimensions=384, distance="cosine"):
    return {"table_name": "t_winner", "index_name": "i_winner", "dimensions": dimensions, "distance": distance, "vector_type": "vector"}


# --- GI-27 -------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    asyncpg.UniqueViolationError('duplicate key value violates unique constraint "primer_collections_pkey"'),
    asyncpg.DuplicateTableError("relation already exists"),
])
async def test_the_loser_of_a_concurrent_create_reuses_the_winners_collection(error):
    conn = _Conn(rows=(None, _winner()), insert_error=error if isinstance(error, asyncpg.UniqueViolationError) else None,
                 ddl_error=error if isinstance(error, asyncpg.DuplicateTableError) else None)
    store = _store(conn)
    await store.create_collection("notes", dimensions=384)
    meta = store._collections["notes"]  # noqa: SLF001
    assert (meta.table_name, meta.index_name, meta.dimensions, meta.distance) == ("t_winner", "i_winner", 384, "cosine")


async def test_the_loser_sees_a_conflict_when_the_winner_made_another_shape():
    conn = _Conn(rows=(None, _winner(dimensions=768)), insert_error=asyncpg.UniqueViolationError("duplicate key"))
    with pytest.raises(ConflictError):
        await _store(conn).create_collection("notes", dimensions=384)


async def test_a_violation_with_no_row_behind_it_is_still_a_failure():
    conn = _Conn(rows=(None,), insert_error=asyncpg.UniqueViolationError("duplicate key"))
    with pytest.raises(ProviderError):
        await _store(conn).create_collection("notes", dimensions=384)


# --- GI-29 -------------------------------------------------------------------------------------------------------------------------------------

def _with_collection(conn, **cfg):
    store = _store(conn, **cfg)
    store._collections["notes"] = _CollectionMeta(table_name="t", index_name="i", dimensions=3, distance="cosine")  # noqa: SLF001
    return store


async def test_a_search_for_more_than_ef_search_raises_it_for_that_query():
    conn = _Conn()
    await _with_collection(conn).search("notes", [0.1, 0.2, 0.3], 500)
    assert "SET LOCAL hnsw.ef_search = 500" in conn.executed and conn.transactions == 1
    assert conn.fetched[-1][1][1] == 500


async def test_the_candidate_list_is_capped_at_pgvectors_limit():
    conn = _Conn()
    await _with_collection(conn).search("notes", [0.1, 0.2, 0.3], 5000)
    assert "SET LOCAL hnsw.ef_search = 1000" in conn.executed


async def test_a_search_within_ef_search_issues_no_extra_statement():
    conn = _Conn()
    await _with_collection(conn).search("notes", [0.1, 0.2, 0.3], 100)
    assert conn.executed == [] and conn.transactions == 0 and len(conn.fetched) == 1


async def test_initialize_applies_the_guc_after_the_extension_and_recycles_earlier_connections(monkeypatch):
    events: list[str] = []

    class _InitConn(_Conn):
        async def execute(self, sql, *args):
            events.append(sql)

    conn = _InitConn()

    class _P(_Pool):
        async def expire_connections(self):
            events.append("expire_connections")

        async def close(self):
            return None

    async def create_pool(**kwargs):
        return _P(conn)

    async def register(_conn):
        events.append("register_vector")

    monkeypatch.setattr("primer.vector.pgvector.asyncpg.create_pool", create_pool)
    monkeypatch.setattr("primer.vector.pgvector.register_vector", register)
    provider = PgVectorStoreProvider(_cfg())
    await provider.initialize()
    after_extension = events[events.index('CREATE EXTENSION IF NOT EXISTS "vector"'):]
    assert "SET hnsw.ef_search = 100" in after_extension and "expire_connections" in after_extension


# --- GI-22 -------------------------------------------------------------------------------------------------------------------------------------

def test_the_hnsw_bounds_are_pgvectors():
    assert hnsw_bound_violation(_cfg()) is None
    assert hnsw_bound_violation(_cfg(hnsw_m=2, hnsw_ef_construction=4, hnsw_ef_search=1)) is None
    assert hnsw_bound_violation(_cfg(hnsw_m=100, hnsw_ef_construction=1000, hnsw_ef_search=1000)) is None
    assert "hnsw_m" in hnsw_bound_violation(_cfg(hnsw_m=101))
    assert "hnsw_m" in hnsw_bound_violation(_cfg(hnsw_m=1))
    assert "hnsw_ef_construction" in hnsw_bound_violation(_cfg(hnsw_ef_construction=1001))
    assert "hnsw_ef_construction" in hnsw_bound_violation(_cfg(hnsw_ef_construction=3))
    assert "2 * hnsw_m" in hnsw_bound_violation(_cfg(hnsw_m=50, hnsw_ef_construction=64))
    assert "hnsw_ef_search" in hnsw_bound_violation(_cfg(hnsw_ef_search=1001))


async def test_the_system_tool_door_refuses_it_too():
    from primer.model.provider import SemanticSearchProvider
    from primer.toolset._system_guards import CrudGuards, refuse_create

    guards = CrudGuards(kind="semantic_search_provider", validate=lambda e: hnsw_bound_violation(e.config) if hasattr(e, "config") else None)
    row = SemanticSearchProvider(id="x", provider="pgvector", config=_cfg(hnsw_m=200))
    refusal = refuse_create(guards, row)
    assert refusal is not None and refusal.is_error and "hnsw_m" in refusal.output
