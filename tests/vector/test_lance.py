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
