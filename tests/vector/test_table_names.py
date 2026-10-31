"""Vector table names are collision-free and deterministic (review of knowledge, finding 10).

``team-notes`` and ``team_notes`` used to map to the same table: on pgvector the second collection reused the first's table (its search returned the
other's chunks across owners), on Lance it could never be indexed; an id with a dot (``v1.2-docs``, valid) was refused by both. The name is now
a short slug of the id plus a hash of the EXACT id. Collections created before keep the table the catalogue stored for them.
"""

from __future__ import annotations

import pytest

from primer.model.except_ import BadRequestError
from primer.vector.names import table_name_for_collection


def test_ids_that_used_to_collide_get_separate_names():
    assert table_name_for_collection("team-notes") != table_name_for_collection("team_notes")
    assert table_name_for_collection("a.b") != table_name_for_collection("a_b") != table_name_for_collection("a-b")


def test_the_name_is_deterministic_and_a_safe_identifier():
    name = table_name_for_collection("v1.2-docs")
    assert name == table_name_for_collection("v1.2-docs")
    assert name.startswith("embeddings_v1_2_docs_") and name.replace("_", "").isalnum()


def test_a_long_id_stays_inside_postgres_identifier_room():
    # 63 bytes is the identifier limit and the index names add up to 9 more: two long ids sharing a prefix must still differ.
    a, b = table_name_for_collection("x" * 200 + "a"), table_name_for_collection("x" * 200 + "b")
    assert a != b and len(a) <= 54 and len(b) <= 54


@pytest.mark.parametrize("bad", ["", "col$bad", "a b", "a/b", "é"])
def test_an_id_outside_the_alphabet_is_still_refused(bad):
    with pytest.raises(BadRequestError):
        table_name_for_collection(bad)


def test_both_backends_use_the_one_function():
    from primer.vector import lance, pgvector

    assert lance._table_name_for_collection("team-notes") == pgvector._table_name_for_collection("team-notes") == table_name_for_collection("team-notes")


pytest.importorskip("lancedb")

from primer.model.provider import LanceConfig  # noqa: E402
from primer.model.vector import EmbeddingRecord  # noqa: E402
from primer.vector.lance import LanceVectorStoreProvider  # noqa: E402


@pytest.fixture
async def lance(tmp_path):
    p = LanceVectorStoreProvider(LanceConfig(path=tmp_path / "lance"))
    await p.initialize()
    try:
        yield p
    finally:
        await p.aclose()


def _record(collection_id: str, text: str) -> EmbeddingRecord:
    return EmbeddingRecord(collection_id=collection_id, document_id="d", chunk_id="0", text=text, vector=[1.0, 0.0, 0.0, 0.0], meta={})


@pytest.mark.asyncio
async def test_two_colliding_ids_are_two_tables_that_do_not_see_each_other(lance):
    store = lance.get_vector_store()
    await store.create_collection("team-notes", dimensions=4)
    await store.create_collection("team_notes", dimensions=4)  # used to raise ProviderError: the table existed
    await store.put(_record("team-notes", "alice private"))
    mine = await store.search("team-notes", [1.0, 0.0, 0.0, 0.0], 5)
    theirs = await store.search("team_notes", [1.0, 0.0, 0.0, 0.0], 5)
    assert [h.record.text for h in mine] == ["alice private"] and theirs == []
    tables = {r["table_name"] for r in await lance._read_catalogue()}
    assert len(tables) == 2


@pytest.mark.asyncio
async def test_a_dotted_id_can_be_indexed_and_searched(lance):
    store = lance.get_vector_store()
    await store.create_collection("v1.2-docs", dimensions=4)
    await store.put(_record("v1.2-docs", "dotted"))
    assert [h.record.text for h in await store.search("v1.2-docs", [1.0, 0.0, 0.0, 0.0], 3)] == ["dotted"]


@pytest.mark.asyncio
async def test_a_collection_made_before_keeps_its_stored_table(lance):
    """An old catalogue row names its table ``embeddings_team_notes``: reads and writes go through that name, not a recomputed one."""
    store = lance.get_vector_store()
    schema_table = "embeddings_legacy_col"
    import pyarrow as pa

    from primer.vector.lance import _record_schema

    await lance.db.create_table(schema_table, schema=_record_schema(4))
    await lance._catalogue_insert(collection_id="legacy-col", table_name=schema_table, dimensions=4, distance="cosine")
    await store.put(_record("legacy-col", "old row"))
    assert [h.record.text for h in await store.search("legacy-col", [1.0, 0.0, 0.0, 0.0], 3)] == ["old row"]
    assert await lance._catalogue_get("legacy-col") is not None and pa is not None
