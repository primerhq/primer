"""Unit tests for matrix.vector.lance.LanceVectorStoreProvider + LanceVectorStore.

Runs against a tmp_path LanceDB. Skipped-soft when the lancedb package
is unavailable so test gates surface the dependency at the right place.
"""

from __future__ import annotations

import pytest

pytest.importorskip("lancedb")  # type: ignore[arg-type]

from primer.model.provider import LanceConfig
from primer.vector.lance import LanceVectorStoreProvider


# ---------- Lifecycle -----------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_creates_directory_and_catalogue(tmp_path):
    cfg_path = tmp_path / "lance-store"
    assert not cfg_path.exists()

    p = LanceVectorStoreProvider(LanceConfig(path=cfg_path))
    try:
        await p.initialize()
        assert cfg_path.exists() and cfg_path.is_dir()
        # Catalogue table is materialised lazily on first reference; the
        # directory existing post-initialize is the contract surface.
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_initialize_is_idempotent(tmp_path):
    p = LanceVectorStoreProvider(LanceConfig(path=tmp_path / "lance"))
    try:
        await p.initialize()
        await p.initialize()  # must not raise
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_aclose_is_idempotent(tmp_path):
    p = LanceVectorStoreProvider(LanceConfig(path=tmp_path / "lance"))
    await p.initialize()
    await p.aclose()
    await p.aclose()  # second close is a no-op


@pytest.mark.asyncio
async def test_get_vector_store_returns_cached_singleton(tmp_path):
    p = LanceVectorStoreProvider(LanceConfig(path=tmp_path / "lance"))
    try:
        await p.initialize()
        store_a = p.get_vector_store()
        store_b = p.get_vector_store()
        assert store_a is store_b
    finally:
        await p.aclose()


# ---------- create_collection ---------------------------------------------


@pytest.fixture
async def lance_provider(tmp_path):
    """Started provider + auto-aclose teardown."""
    p = LanceVectorStoreProvider(LanceConfig(path=tmp_path / "lance"))
    await p.initialize()
    try:
        yield p
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_create_collection_basic(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=4)
    # Catalogue row visible via the internal _catalogue helper.
    rows = await lance_provider._read_catalogue()
    assert len(rows) == 1
    assert rows[0]["collection_id"] == "col-a"
    assert rows[0]["dimensions"] == 4
    assert rows[0]["distance"] == "cosine"
    assert rows[0]["indexed"] is False


@pytest.mark.asyncio
async def test_create_collection_idempotent_same_args(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=4)
    # Same args: no-op, no error.
    await store.create_collection("col-a", dimensions=4, distance="cosine")
    rows = await lance_provider._read_catalogue()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_create_collection_dimension_mismatch_conflict(lance_provider):
    from primer.model.except_ import ConflictError

    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=4)
    with pytest.raises(ConflictError):
        await store.create_collection("col-a", dimensions=8)


@pytest.mark.asyncio
async def test_create_collection_bad_id_rejected(lance_provider):
    from primer.model.except_ import BadRequestError

    store = lance_provider.get_vector_store()
    with pytest.raises(BadRequestError):
        await store.create_collection("col$with$bad$chars", dimensions=4)


@pytest.mark.asyncio
async def test_create_collection_ip_distance_maps_to_dot(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=4, distance="ip")
    rows = await lance_provider._read_catalogue()
    assert rows[0]["distance"] == "dot"


# ---------- put / get / delete --------------------------------------------


def _record(*, doc, chunk, vec, text="t", meta=None):
    from primer.model.vector import EmbeddingRecord
    return EmbeddingRecord(
        collection_id="col-a",
        document_id=doc,
        chunk_id=chunk,
        text=text,
        vector=vec,
        meta=meta or {},
    )


@pytest.mark.asyncio
async def test_put_then_get_round_trips(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    rec = _record(doc="d1", chunk="c1", vec=[0.1, 0.2, 0.3],
                  text="hello", meta={"k": "v"})
    await store.put(rec)

    got = await store.get("col-a", "d1")
    assert len(got) == 1
    assert got[0].document_id == "d1"
    assert got[0].chunk_id == "c1"
    assert got[0].text == "hello"
    assert got[0].meta == {"k": "v"}
    assert list(got[0].vector) == pytest.approx([0.1, 0.2, 0.3])


@pytest.mark.asyncio
async def test_put_upserts_on_composite_key(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    await store.put(_record(doc="d1", chunk="c1", vec=[0.1, 0.2, 0.3], text="v1"))
    await store.put(_record(doc="d1", chunk="c1", vec=[0.9, 0.8, 0.7], text="v2"))
    got = await store.get("col-a", "d1")
    assert len(got) == 1
    assert got[0].text == "v2"
    assert list(got[0].vector) == pytest.approx([0.9, 0.8, 0.7])


@pytest.mark.asyncio
async def test_get_orders_by_chunk_id(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    for cid in ["c-3", "c-1", "c-2"]:
        await store.put(_record(doc="d1", chunk=cid, vec=[0.0, 0.0, 0.0]))
    got = await store.get("col-a", "d1")
    assert [r.chunk_id for r in got] == ["c-1", "c-2", "c-3"]


@pytest.mark.asyncio
async def test_get_missing_document_returns_empty(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    assert await store.get("col-a", "missing-doc") == []


@pytest.mark.asyncio
async def test_delete_removes_all_chunks(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    await store.put(_record(doc="d1", chunk="c1", vec=[0.1, 0.1, 0.1]))
    await store.put(_record(doc="d1", chunk="c2", vec=[0.2, 0.2, 0.2]))
    await store.delete("col-a", "d1")
    assert await store.get("col-a", "d1") == []


@pytest.mark.asyncio
async def test_delete_is_idempotent(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    await store.delete("col-a", "never-existed")  # no error


# ---------- search --------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_top_k_ordered_by_similarity(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    await store.put(_record(doc="d1", chunk="c1", vec=[1.0, 0.0, 0.0], text="x-axis"))
    await store.put(_record(doc="d2", chunk="c1", vec=[0.0, 1.0, 0.0], text="y-axis"))
    await store.put(_record(doc="d3", chunk="c1", vec=[0.0, 0.0, 1.0], text="z-axis"))

    hits = await store.search("col-a", [0.95, 0.05, 0.0], k=2)
    assert len(hits) == 2
    # Top hit should be the x-axis vector (highest cosine sim).
    assert hits[0].record.document_id == "d1"
    # Scores monotone non-increasing.
    assert hits[0].score is None or hits[0].score >= hits[1].score


@pytest.mark.asyncio
async def test_search_empty_collection_returns_empty(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    hits = await store.search("col-a", [0.1, 0.2, 0.3], k=5)
    assert hits == []


# ---------- search_by_meta -----------------------------------------------


@pytest.mark.asyncio
async def test_search_by_meta_flat_keys(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    await store.put(_record(doc="d1", chunk="c1", vec=[0.0]*3, meta={"src": "a"}))
    await store.put(_record(doc="d2", chunk="c1", vec=[0.0]*3, meta={"src": "b"}))
    await store.put(_record(doc="d3", chunk="c1", vec=[0.0]*3, meta={"src": "a"}))
    hits = await store.search_by_meta("col-a", {"src": "a"})
    assert sorted(r.document_id for r in hits) == ["d1", "d3"]


@pytest.mark.asyncio
async def test_search_by_meta_empty_returns_all(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    await store.put(_record(doc="d1", chunk="c1", vec=[0.0]*3))
    await store.put(_record(doc="d2", chunk="c1", vec=[0.0]*3))
    hits = await store.search_by_meta("col-a", {})
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_search_by_meta_nested(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    await store.put(_record(doc="d1", chunk="c1", vec=[0.0]*3,
                            meta={"src": {"kind": "wiki"}}))
    await store.put(_record(doc="d2", chunk="c1", vec=[0.0]*3,
                            meta={"src": {"kind": "rss"}}))
    hits = await store.search_by_meta("col-a", {"src": {"kind": "wiki"}})
    assert [r.document_id for r in hits] == ["d1"]


@pytest.mark.asyncio
async def test_unknown_collection_raises_bad_request(lance_provider):
    from primer.model.except_ import BadRequestError
    store = lance_provider.get_vector_store()
    with pytest.raises(BadRequestError):
        await store.put(_record(doc="d1", chunk="c1", vec=[0.0, 0.0, 0.0]))
    with pytest.raises(BadRequestError):
        await store.search("missing-coll", [0.0, 0.0, 0.0], k=1)
    with pytest.raises(BadRequestError):
        await store.get("missing-coll", "d1")


# ---------- lazy HNSW index ----------------------------------------------


@pytest.mark.asyncio
async def test_index_built_when_row_count_crosses_threshold(tmp_path):
    # Construct the provider with a tiny threshold so the test is fast.
    cfg = LanceConfig(path=tmp_path / "lance", index_min_rows=3)
    p = LanceVectorStoreProvider(cfg)
    await p.initialize()
    try:
        store = p.get_vector_store()
        await store.create_collection("col-a", dimensions=3)
        # Below threshold — index should NOT be built.
        await store.put(_record(doc="d1", chunk="c1", vec=[0.1, 0.0, 0.0]))
        await store.put(_record(doc="d2", chunk="c1", vec=[0.0, 0.1, 0.0]))
        row = await p._catalogue_get("col-a")
        assert row["indexed"] is False
        # Crossing the threshold — index MUST be built.
        await store.put(_record(doc="d3", chunk="c1", vec=[0.0, 0.0, 0.1]))
        row = await p._catalogue_get("col-a")
        assert row["indexed"] is True
    finally:
        await p.aclose()


# ---------- HNSW config knobs ------------------------------------------


@pytest.mark.asyncio
async def test_hnsw_custom_knobs_reach_index(tmp_path):
    """Custom hnsw_m and ef_construction are passed to the index builder.

    LanceDB 0.30.2 does not expose m/ef_construction on list_indices()
    output, so we verify the code path (index builds without error and
    catalogue marks indexed=True) rather than introspecting the stored
    values.
    """
    cfg = LanceConfig(
        path=tmp_path / "lance",
        hnsw_m=8,
        hnsw_ef_construction=32,
        hnsw_ef_search=20,
        index_min_rows=2,
    )
    p = LanceVectorStoreProvider(cfg)
    await p.initialize()
    try:
        store = p.get_vector_store()
        await store.create_collection("col-a", dimensions=3)
        # Put enough rows to cross the threshold and trigger _build_index.
        await store.put(_record(doc="d1", chunk="c1", vec=[0.1, 0.0, 0.0]))
        await store.put(_record(doc="d2", chunk="c1", vec=[0.0, 0.1, 0.0]))
        row = await p._catalogue_get("col-a")
        assert row["indexed"] is True, "index must be built after crossing threshold"
        # Search with ef_search knob must not raise.
        hits = await store.search("col-a", [1.0, 0.0, 0.0], k=2)
        assert len(hits) == 2
    finally:
        await p.aclose()


# ---------- SQL-safety on document_id ----------------------------------


@pytest.mark.asyncio
async def test_document_id_with_apostrophe_round_trips(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    weird = "O'Brien-doc"
    await store.put(_record(doc=weird, chunk="c1", vec=[0.1, 0.2, 0.3]))
    got = await store.get("col-a", weird)
    assert len(got) == 1
    assert got[0].document_id == weird
    # delete also works
    await store.delete("col-a", weird)
    assert await store.get("col-a", weird) == []


# ---------- maintain_indexes ---------------------------------------------


@pytest.mark.asyncio
async def test_maintain_indexes_returns_one_report_per_collection(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=3)
    await store.create_collection("col-b", dimensions=3)
    reports = await lance_provider.maintain_indexes()
    cids = sorted(r.collection_id for r in reports)
    assert cids == ["col-a", "col-b"]
    for r in reports:
        assert r.duration_seconds >= 0
        assert r.started_at is not None


@pytest.mark.asyncio
async def test_maintain_indexes_reports_reindex_when_indexed(tmp_path):
    cfg = LanceConfig(path=tmp_path / "lance", index_min_rows=2)
    p = LanceVectorStoreProvider(cfg)
    await p.initialize()
    try:
        store = p.get_vector_store()
        await store.create_collection("col-a", dimensions=3)
        await store.put(_record(doc="d1", chunk="c1", vec=[0.1, 0.0, 0.0]))
        await store.put(_record(doc="d2", chunk="c1", vec=[0.0, 0.1, 0.0]))
        # Threshold crossed → indexed=true.
        reports = await p.maintain_indexes()
        assert len(reports) == 1
        assert reports[0].action == "reindex"
    finally:
        await p.aclose()
