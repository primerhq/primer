"""Unit tests for matrix.vector.lance.LanceVectorStoreProvider + LanceVectorStore.

Runs against a tmp_path LanceDB. Skipped-soft when the lancedb package
is unavailable so test gates surface the dependency at the right place.
"""

from __future__ import annotations

import pytest

pytest.importorskip("lancedb")  # type: ignore[arg-type]

from matrix.model.provider import LanceConfig
from matrix.vector.lance import LanceVectorStoreProvider


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
    from matrix.model.except_ import ConflictError

    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=4)
    with pytest.raises(ConflictError):
        await store.create_collection("col-a", dimensions=8)


@pytest.mark.asyncio
async def test_create_collection_bad_id_rejected(lance_provider):
    from matrix.model.except_ import BadRequestError

    store = lance_provider.get_vector_store()
    with pytest.raises(BadRequestError):
        await store.create_collection("col$with$bad$chars", dimensions=4)


@pytest.mark.asyncio
async def test_create_collection_ip_distance_maps_to_dot(lance_provider):
    store = lance_provider.get_vector_store()
    await store.create_collection("col-a", dimensions=4, distance="ip")
    rows = await lance_provider._read_catalogue()
    assert rows[0]["distance"] == "dot"
