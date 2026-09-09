import os
import uuid

import asyncpg
import pytest

from primer.model.except_ import BadRequestError
from primer.model.provider import PgVectorConfig
from primer.model.vector import EmbeddingRecord
from primer.vector.pgvector import PgVectorStoreProvider

pytestmark = pytest.mark.asyncio

# scripts/e2e/bringup.sh creates `primer_e2e` with the pgvector extension
# already enabled (see its own docstring, step 2) and publishes postgres on
# PRIMER_DB_PORT (default 5432) - the same variable every tests/e2e/*.py DB
# helper reads (e.g. test_multi_cycle_resume_stability_journey.py's own
# _pg_port()). This file used to hardcode port 5432 and a "primer_dogfood"
# database that no bringup script anywhere creates, with no env override at
# all - so it could only ever run by chance against a same-named database a
# developer happened to have lying around, and _pg_up() swallowing every
# exception meant it skipped SILENTLY everywhere else, CI included.
_DB_NAME = "primer_e2e"


def _pg_port() -> int:
    return int(os.environ.get("PRIMER_DB_PORT", "5432"))


async def _pg_unreachable_reason() -> str | None:
    """None if the e2e postgres is reachable, else a reason worth reading -
    a bare "not reachable" skip already hid a broken host/db/port for
    every run before this fix existed."""
    try:
        conn = await asyncpg.connect(
            host="localhost", port=_pg_port(), user="primer",
            password="primer", database=_DB_NAME, timeout=3,
        )
        await conn.close()
        return None
    except Exception as exc:  # noqa: BLE001 - surfaced in the skip reason, not swallowed
        return f"{type(exc).__name__}: {exc}"


async def _provider(*, use_halfvec: bool, schema: str) -> PgVectorStoreProvider:
    cfg = PgVectorConfig(
        hostname="localhost", port=_pg_port(), username="primer",
        password="primer", database=_DB_NAME,
        db_schema=schema, use_halfvec=use_halfvec,
    )
    p = PgVectorStoreProvider(cfg)
    await p.initialize()
    return p


async def _drop_schema(schema: str) -> None:
    conn = await asyncpg.connect(
        host="localhost", port=_pg_port(), user="primer",
        password="primer", database=_DB_NAME,
    )
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await conn.close()


def _vec(n: int, seed: float) -> list[float]:
    return [(seed + i) % 1.0 for i in range(n)]


async def test_halfvec_create_put_search_3072():
    reason = await _pg_unreachable_reason()
    if reason:
        pytest.skip(f"e2e postgres not reachable on port {_pg_port()}: {reason}")
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
    reason = await _pg_unreachable_reason()
    if reason:
        pytest.skip(f"e2e postgres not reachable on port {_pg_port()}: {reason}")
    schema = "halfvec_test_" + uuid.uuid4().hex[:8]
    p = await _provider(use_halfvec=False, schema=schema)
    try:
        store = p.get_vector_store()
        with pytest.raises(BadRequestError) as exc:
            await store.create_collection("big", dimensions=3072)
        assert "use_halfvec" in str(exc.value)
    finally:
        await _drop_schema(schema)


async def test_recreate_existing_halfvec_collection_ignores_flipped_flag():
    # Flag only affects NEW collections: re-creating an existing 3072-dim
    # halfvec collection through a provider whose use_halfvec was later turned
    # off must return the existing collection, not reject on the 2000 ceiling.
    reason = await _pg_unreachable_reason()
    if reason:
        pytest.skip(f"e2e postgres not reachable on port {_pg_port()}: {reason}")
    schema = "halfvec_test_" + uuid.uuid4().hex[:8]
    p1 = await _provider(use_halfvec=True, schema=schema)
    try:
        await p1.get_vector_store().create_collection(
            "c", dimensions=3072, distance="cosine")
        # Fresh provider, same schema, flag flipped off -> cold _collections
        # cache forces the catalogue-lookup short-circuit path.
        p2 = await _provider(use_halfvec=False, schema=schema)
        store2 = p2.get_vector_store()
        # Must not raise on the 2000 ceiling for the pre-existing collection.
        await store2.create_collection("c", dimensions=3072, distance="cosine")
        assert store2._collections["c"].vector_type == "halfvec"
    finally:
        await _drop_schema(schema)


async def test_standard_vector_collection_still_works():
    reason = await _pg_unreachable_reason()
    if reason:
        pytest.skip(f"e2e postgres not reachable on port {_pg_port()}: {reason}")
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
