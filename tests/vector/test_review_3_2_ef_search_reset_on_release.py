"""Review 2026-11-23 finding 3.2: ``hnsw.ef_search`` is undone after a pooled connection's first use.

The GUC is set only in the asyncpg pool ``init`` hook (``_init_connection`` -> ``_apply_search_guc``,
``SET hnsw.ef_search = ...`` on every freshly OPENED connection). asyncpg's pool release resets a
used connection (``RESET ALL``), so after the connection's first use it runs at pgvector's stock
``ef_search`` of 40. ``candidate_list_sql`` then skips its per-query raise for every ``k`` within
the CONFIGURED value (``k <= configured`` -> ``None``), so nothing corrects it: a search that
configured ``ef_search=100`` runs at 40 after warm-up. This test builds the real store with
``hnsw_ef_search=100`` on a pool of size 1, runs one search (releasing the connection), then reads
``SHOW hnsw.ef_search`` through the store's own pool and asserts it is still the configured value
(the Postgres lane runs it; it skips without ``PRIMER_TEST_POSTGRES_URL``).
"""

import uuid

import pytest

from primer.model.providers.storage import PoolConfig
from primer.model.provider import PgVectorConfig
from primer.vector.pgvector import PgVectorStoreProvider
from tests.pg_gate import postgres_marks
from tests.vector.test_halfvec_e2e import _conn_args, _drop_schema

pytestmark = [pytest.mark.asyncio, *postgres_marks("the live pgvector ef_search GUC tests")]

EF_SEARCH = 100


async def _provider(schema: str) -> PgVectorStoreProvider:
    a = _conn_args()
    cfg = PgVectorConfig(
        hostname=a["host"], port=a["port"], username=a["user"],
        password=a["password"], database=a["database"],
        db_schema=schema,
        hnsw_ef_search=EF_SEARCH,
        pool=PoolConfig(min_size=1, max_size=1),
    )
    p = PgVectorStoreProvider(cfg)
    await p.initialize()
    return p


async def test_ef_search_survives_a_connection_release():
    schema = "efsearch_test_" + uuid.uuid4().hex[:8]
    p = await _provider(schema)
    try:
        store = p.get_vector_store()
        await store.create_collection("ef", dimensions=3)
        from primer.model.vector import EmbeddingRecord

        await store.put(EmbeddingRecord(collection_id="ef", document_id="d", chunk_id="0",
                                         text="warm-up", vector=[1.0, 0.0, 0.0], meta={}))

        # One search: the single pooled connection is used and released, and
        # asyncpg's release runs RESET ALL on it.
        await store.search("ef", [1.0, 0.0, 0.0], k=3)

        # The store's own connection path must still see the configured GUC,
        # not pgvector's stock 40: every later search (and every k <=
        # configured, which candidate_list_sql does not raise per query) runs
        # on whatever this session carries.
        async with p.pool.acquire() as conn:
            got = int(await conn.fetchval("SHOW hnsw.ef_search"))
        assert got == EF_SEARCH, (
            f"hnsw.ef_search read back {got}, not the configured {EF_SEARCH}: the pool init hook "
            f"sets the GUC only when a connection is opened, and asyncpg's release RESET ALLs a "
            f"used connection, so after the first use searches run at pgvector's stock 40"
        )
    finally:
        await p.aclose()
        await _drop_schema(schema)
