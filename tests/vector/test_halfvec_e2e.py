import uuid

import asyncpg
import pytest

from primer.model.except_ import BadRequestError
from primer.model.provider import PgVectorConfig
from primer.model.vector import EmbeddingRecord
from primer.vector.pgvector import PgVectorStoreProvider

pytestmark = pytest.mark.asyncio

_DSN = dict(hostname="localhost", port=5432, username="primer",
            password="primer", database="primer_dogfood")


async def _pg_up() -> bool:
    try:
        conn = await asyncpg.connect(
            host="localhost", port=5432, user="primer",
            password="primer", database="primer_dogfood", timeout=3,
        )
        await conn.close()
        return True
    except Exception:
        return False


async def _provider(*, use_halfvec: bool, schema: str) -> PgVectorStoreProvider:
    cfg = PgVectorConfig(**_DSN, db_schema=schema, use_halfvec=use_halfvec)
    p = PgVectorStoreProvider(cfg)
    await p.initialize()
    return p


async def _drop_schema(schema: str) -> None:
    conn = await asyncpg.connect(
        host="localhost", port=5432, user="primer",
        password="primer", database="primer_dogfood",
    )
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await conn.close()


def _vec(n: int, seed: float) -> list[float]:
    return [(seed + i) % 1.0 for i in range(n)]


async def test_halfvec_create_put_search_3072():
    if not await _pg_up():
        pytest.skip("dogfood pg not reachable")
    schema = "halfvec_test_" + uuid.uuid4().hex[:8]
    p = await _provider(use_halfvec=True, schema=schema)
    try:
        store = p.get_vector_store()
        await store.create_collection("c", dimensions=3072, distance="cosine")
        async with p.pool.acquire() as conn:
            col_type = await conn.fetchval(
                "SELECT a.atttypid::regtype::text FROM pg_attribute a "
                "JOIN pg_class c ON c.oid=a.attrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=$1 AND c.relname='embeddings_c' AND a.attname='vector'",
                schema,
            )
            assert col_type == "halfvec"
            vtype = await conn.fetchval(
                f'SELECT vector_type FROM "{schema}".primer_collections WHERE collection_id=$1', "c",
            )
            assert vtype == "halfvec"
        await store.put(EmbeddingRecord(collection_id="c", document_id="d1", chunk_id="0",
                                        text="hello", vector=_vec(3072, 0.1), meta={}))
        await store.put(EmbeddingRecord(collection_id="c", document_id="d2", chunk_id="0",
                                        text="world", vector=_vec(3072, 0.5), meta={}))
        results = await store.search("c", _vec(3072, 0.1), k=2)
        assert len(results) == 2
        assert results[0].record.document_id in {"d1", "d2"}
    finally:
        await _drop_schema(schema)


async def test_vector_provider_rejects_over_2000_without_halfvec():
    if not await _pg_up():
        pytest.skip("dogfood pg not reachable")
    schema = "halfvec_test_" + uuid.uuid4().hex[:8]
    p = await _provider(use_halfvec=False, schema=schema)
    try:
        store = p.get_vector_store()
        with pytest.raises(BadRequestError) as exc:
            await store.create_collection("big", dimensions=3072)
        assert "use_halfvec" in str(exc.value)
    finally:
        await _drop_schema(schema)


async def test_standard_vector_collection_still_works():
    if not await _pg_up():
        pytest.skip("dogfood pg not reachable")
    schema = "halfvec_test_" + uuid.uuid4().hex[:8]
    p = await _provider(use_halfvec=False, schema=schema)
    try:
        store = p.get_vector_store()
        await store.create_collection("std", dimensions=1536)
        async with p.pool.acquire() as conn:
            vtype = await conn.fetchval(
                f'SELECT vector_type FROM "{schema}".primer_collections WHERE collection_id=$1', "std",
            )
            assert vtype == "vector"
        await store.put(EmbeddingRecord(collection_id="std", document_id="d1", chunk_id="0",
                                        text="x", vector=_vec(1536, 0.2), meta={}))
        assert len(await store.search("std", _vec(1536, 0.2), k=1)) == 1
    finally:
        await _drop_schema(schema)
