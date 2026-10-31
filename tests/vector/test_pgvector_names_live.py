"""Collision-free pgvector table names on a real pgvector (review of knowledge, finding 10; the Postgres lane runs these).

``team-notes`` and ``team_notes`` used to share one table (a search of one returned the other's chunks); a dotted id could not be created; and a
collection made under the old naming must keep its stored table.
"""

import uuid

import pytest

from primer.model.vector import EmbeddingRecord
from tests.pg_gate import postgres_marks
from tests.vector.test_halfvec_e2e import _drop_schema, _provider

pytestmark = [pytest.mark.asyncio, *postgres_marks("the live pgvector table name tests")]


def _rec(collection_id: str, text: str) -> EmbeddingRecord:
    return EmbeddingRecord(collection_id=collection_id, document_id="d", chunk_id="0", text=text, vector=[1.0, 0.0, 0.0], meta={})


async def test_two_colliding_ids_do_not_share_a_table_or_their_chunks():
    schema = "names_test_" + uuid.uuid4().hex[:8]
    p = await _provider(use_halfvec=False, schema=schema)
    try:
        store = p.get_vector_store()
        await store.create_collection("team-notes", dimensions=3)
        await store.create_collection("team_notes", dimensions=3)
        await store.put(_rec("team-notes", "alice private"))
        assert [h.record.text for h in await store.search("team-notes", [1.0, 0.0, 0.0], k=5)] == ["alice private"]
        assert await store.search("team_notes", [1.0, 0.0, 0.0], k=5) == []  # used to return Alice's chunk to Bob
        async with p.pool.acquire() as conn:
            tables = {r["table_name"] for r in await conn.fetch(f'SELECT table_name FROM "{schema}".primer_collections')}
        assert len(tables) == 2
    finally:
        await _drop_schema(schema)


async def test_a_dotted_id_can_be_created_written_and_searched():
    schema = "names_test_" + uuid.uuid4().hex[:8]
    p = await _provider(use_halfvec=False, schema=schema)
    try:
        store = p.get_vector_store()
        await store.create_collection("v1.2-docs", dimensions=3)
        await store.put(_rec("v1.2-docs", "dotted"))
        assert [h.record.text for h in await store.search("v1.2-docs", [1.0, 0.0, 0.0], k=3)] == ["dotted"]
    finally:
        await _drop_schema(schema)


async def test_a_collection_made_under_the_old_naming_keeps_its_stored_table():
    """The catalogue row names the table; a cold provider (empty cache) re-creating the id adopts THAT name, not a recomputed one."""
    schema = "names_test_" + uuid.uuid4().hex[:8]
    p = await _provider(use_halfvec=False, schema=schema)
    try:
        store = p.get_vector_store()
        await store.create_collection("seed", dimensions=3)
        # Rewrite the row as the old naming would have left it: table embeddings_legacy_notes.
        async with p.pool.acquire() as conn:
            row = await conn.fetchrow(f'SELECT table_name, index_name FROM "{schema}".primer_collections WHERE collection_id=$1', "seed")
            await conn.execute(f'ALTER TABLE "{schema}"."{row["table_name"]}" RENAME TO embeddings_legacy_notes')
            await conn.execute(
                f'UPDATE "{schema}".primer_collections SET collection_id=$1, table_name=$2 WHERE collection_id=$3',
                "legacy-notes", "embeddings_legacy_notes", "seed",
            )
        p2 = await _provider(use_halfvec=False, schema=schema)
        store2 = p2.get_vector_store()
        await store2.create_collection("legacy-notes", dimensions=3)  # the existing-row branch
        assert store2._collections["legacy-notes"].table_name == "embeddings_legacy_notes"
        await store2.put(_rec("legacy-notes", "old row"))
        assert [h.record.text for h in await store2.search("legacy-notes", [1.0, 0.0, 0.0], k=3)] == ["old row"]
    finally:
        await _drop_schema(schema)
