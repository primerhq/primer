"""index_document must read the body from the content store, not meta.

P1 moves document bodies into the ``DocumentContentStore``. The indexer
still chunks + embeds on write, but the text it chunks now comes from the
content store keyed by the stable document id - NOT from ``meta['text']``
/ ``meta['content']`` (which the new write path leaves empty)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from primer.knowledge.indexing import index_document
from primer.model.collection import Collection, CollectionEmbedder, Document
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory

# Reuse the existing indexing-test fakes.
from tests.knowledge.test_indexing import _Emb, _Store


def _collection() -> Collection:
    return Collection(
        id="kb-1",
        description="test",
        embedder=CollectionEmbedder(provider_id="emb", model="m"),
        search_provider_id="ssp",
        system=False,
    )


@pytest.mark.asyncio
async def test_index_document_reads_body_from_content_store(tmp_path: Path) -> None:
    """Body lives ONLY in the content store; meta is empty. The chunks the
    vector store receives must derive from the content-store body - the old
    meta-read would have produced zero chunks here."""
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "content.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    try:
        content_store = provider.get_content_store()
        await content_store.ensure_schema()

        # The body lives only in the content store.
        body = "\n\n".join(["a" * 800, "b" * 800])
        await content_store.upsert(
            document_id="doc-1",
            collection_id="kb-1",
            path="doc-1.md",
            content=body,
        )

        # meta is EMPTY - the old meta-read would yield no chunks.
        document = Document(
            id="doc-1", collection_id="kb-1", name="d", path="doc-1.md", meta={}
        )

        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb(dim=4))
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        n = await index_document(
            document=document,
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
            content_store=content_store,
        )

        # Two 800-char paragraphs -> two chunks, all from the content store.
        assert n == 2
        assert len(store.puts) == 2
        recombined = "\n\n".join(p.text for p in store.puts)
        assert recombined == body
        assert all(p.text for p in store.puts)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_index_document_falls_back_to_meta_when_no_content_row(
    tmp_path: Path,
) -> None:
    """Transitional: a document with no content row still indexes from meta."""
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "content.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    try:
        content_store = provider.get_content_store()
        await content_store.ensure_schema()

        document = Document(
            id="doc-2",
            collection_id="kb-1",
            name="d",
            path="doc-2.md",
            meta={"text": "from meta fallback"},
        )

        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        n = await index_document(
            document=document,
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
            content_store=content_store,
        )
        assert n == 1
        assert store.puts[0].text == "from meta fallback"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_index_document_falls_back_to_meta_on_empty_content_row(
    tmp_path: Path,
) -> None:
    """Transition window: the content row exists but is the empty string,
    while the real body still lives in meta. An empty content row is NOT
    None, so the old `is None` guard would index ZERO chunks; the fixed
    guard treats a blank content-store result as a miss and falls back to
    the meta body."""
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "content.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    try:
        content_store = provider.get_content_store()
        await content_store.ensure_schema()

        # An empty-string content row (real body still in meta).
        await content_store.upsert(
            document_id="doc-3",
            collection_id="kb-1",
            path="doc-3.md",
            content="",
        )

        document = Document(
            id="doc-3",
            collection_id="kb-1",
            name="d",
            path="doc-3.md",
            meta={"content": "real body still in meta"},
        )

        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        n = await index_document(
            document=document,
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
            content_store=content_store,
        )
        assert n == 1
        assert store.puts[0].text == "real body still in meta"
    finally:
        await provider.aclose()
