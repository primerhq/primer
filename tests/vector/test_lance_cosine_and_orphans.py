"""T60 (KN-11 D4, KN-14): a Lance collection ranks and scores by ITS metric (cosine) whatever the vectors' norm and whether or not the index exists,
and a table left behind by a crash between create_table and the catalogue insert does not block the collection forever."""

from __future__ import annotations

import math
import random

import pytest

from primer.model.provider import LanceConfig
from primer.model.vector import EmbeddingRecord
from primer.vector.lance import LanceVectorStoreProvider, _record_schema


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def _store(tmp_path, index_min_rows):
    provider = LanceVectorStoreProvider(LanceConfig(path=tmp_path / "lance", index_min_rows=index_min_rows))
    await provider.initialize()
    return provider, provider.get_vector_store()


async def _fill(store, vectors, collection="c"):
    for key, vec in vectors.items():
        await store.put(EmbeddingRecord(collection_id=collection, document_id=key, chunk_id="0", text=key, vector=vec))


async def test_unnormalised_vectors_rank_by_cosine(tmp_path):
    rnd = random.Random(7)
    _, store = await _store(tmp_path, 100000)           # never indexed: brute force
    await store.create_collection("c", dimensions=8, distance="cosine")
    vecs = {str(i): [rnd.uniform(-1, 1) * rnd.choice([0.1, 1, 10]) for _ in range(8)] for i in range(60)}
    await _fill(store, vecs)
    q = [rnd.uniform(-1, 1) for _ in range(8)]
    hits = await store.search("c", q, 5)
    want = sorted(vecs, key=lambda d: -_cos(vecs[d], q))[:5]
    assert [h.record.document_id for h in hits] == want
    for h in hits:
        assert h.score == pytest.approx(_cos(vecs[h.record.document_id], q), abs=1e-4)      # the score IS the cosine


async def test_scores_do_not_shift_when_the_index_is_built(tmp_path):
    rnd = random.Random(3)
    provider, store = await _store(tmp_path, 300)
    await store.create_collection("c", dimensions=8, distance="cosine")
    unit = {}
    for i in range(299):
        v = [rnd.gauss(0, 1) for _ in range(8)]
        n = math.sqrt(sum(x * x for x in v))
        unit[str(i)] = [x / n for x in v]
    await _fill(store, unit)
    assert not (await provider._catalogue_get("c"))["indexed"]
    q = unit["5"]
    before = {h.record.document_id: h.score for h in await store.search("c", q, 3)}
    assert before["5"] == pytest.approx(1.0, abs=1e-4)
    extra = {f"x{i}": [rnd.gauss(0, 1) for _ in range(8)] for i in range(30)}       # past the threshold: the index is built
    await _fill(store, extra)
    assert (await provider._catalogue_get("c"))["indexed"] is True
    after = {h.record.document_id: h.score for h in await store.search("c", q, 3)}
    assert after["5"] == pytest.approx(1.0, abs=1e-3)
    for key in set(before) & set(after):
        assert after[key] == pytest.approx(before[key], abs=1e-3)


async def test_unit_normalised_collections_below_the_threshold_keep_their_scores(tmp_path):
    _, store = await _store(tmp_path, 100000)
    await store.create_collection("c", dimensions=2, distance="cosine")
    await _fill(store, {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [math.sqrt(0.5), math.sqrt(0.5)]})
    scores = {h.record.document_id: h.score for h in await store.search("c", [1.0, 0.0], 3)}
    assert scores["a"] == pytest.approx(1.0, abs=1e-4)
    assert scores["c"] == pytest.approx(math.sqrt(0.5), abs=1e-4)
    assert scores["b"] == pytest.approx(0.0, abs=1e-4)


async def test_a_table_without_a_catalogue_row_does_not_block_the_collection(tmp_path):
    provider, store = await _store(tmp_path, 1000)
    from primer.vector.names import table_name_for_collection

    # the crash window: create_table succeeded, the catalogue insert did not
    await provider.db.create_table(table_name_for_collection("kb"), schema=_record_schema(4))
    await store.create_collection("kb", dimensions=4)
    await _fill(store, {"a": [1.0, 0.0, 0.0, 0.0]}, collection="kb")
    assert [h.record.document_id for h in await store.search("kb", [1.0, 0.0, 0.0, 0.0], 1)] == ["a"]
