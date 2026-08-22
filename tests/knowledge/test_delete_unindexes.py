"""Regression pins for terrain bugs 1-3 (spec section 9 required coverage)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest_asyncio

from primer.knowledge.indexing import index_document, remove_document_index
from primer.knowledge.tree import DocumentTreeService
from primer.model.collection import (
    Collection, CollectionEmbedder, CollectionSearchConfig,
)
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from tests.knowledge.test_indexing import _Emb, _StatefulStore


@pytest_asyncio.fixture
async def indexed_env(tmp_path):
    """A search-enabled collection wired to a real indexer + unindexer over
    fake providers, mirroring build_document_indexer / _unindexer without
    a Request."""
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()

    cid = "c-idx"
    collection = Collection(
        id=cid, description="wiki",
        search=CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="e", model="m"),
            vector_store_provider_id="s",
        ),
    )
    await provider.get_storage(Collection).create(collection)

    store = _StatefulStore()
    reg = AsyncMock()
    reg.get_embedder = AsyncMock(return_value=_Emb(dim=4))
    ssr = AsyncMock()
    ssr.get_store = AsyncMock(return_value=store)

    async def _indexer(*, document, content):
        await index_document(
            document=document, collection=collection,
            provider_registry=reg, semantic_search_registry=ssr,
            content_store=provider.get_content_store(),
        )

    async def _unindexer(*, document_id, collection_id):
        await remove_document_index(
            document_id=document_id, collection=collection,
            semantic_search_registry=ssr,
        )

    tree = DocumentTreeService(provider, indexer=_indexer, unindexer=_unindexer)
    yield tree, store, cid
    await provider.aclose()


async def test_delete_always_unindexes_vectors(indexed_env):
    # bug 1: path-addressed DELETE previously left chunks in the store.
    tree, store, cid = indexed_env
    doc = await tree.create(collection_id=cid, parent="", slug="doomed", body="text")
    assert any(r.document_id == doc.id for r in await store.search_by_meta(cid, meta={}))
    await tree.delete(collection_id=cid, path="doomed")
    assert not any(
        r.document_id == doc.id for r in await store.search_by_meta(cid, meta={})
    )


async def test_delete_always_removes_content_rows(indexed_env):
    # bug 2: generic CRUD delete previously left the content row behind.
    tree, _store, cid = indexed_env
    await tree.create(collection_id=cid, parent="", slug="gone", body="b")
    await tree.delete(collection_id=cid, path="gone")
    assert await tree._content.resolve_id(cid, "gone") is None


async def test_single_chunk_id_convention(indexed_env):
    # bug 3: str(idx) is the ONLY chunk-id shape ('chunk-%06d' died with
    # DocumentIngester).
    tree, store, cid = indexed_env
    await tree.create(collection_id=cid, parent="", slug="pinned", body="text")
    for r in await store.search_by_meta(cid, meta={}):
        assert r.chunk_id.isdigit()
