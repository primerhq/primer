"""Tests for user-document chunking + embedding + indexing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from primer.knowledge.indexing import chunk_text, index_document
from primer.model.collection import Collection, CollectionEmbedder, Document


def _collection(system: bool = False) -> Collection:
    return Collection(
        id="kb-1",
        description="test",
        embedder=CollectionEmbedder(provider_id="emb", model="m"),
        search_provider_id="ssp",
        system=system,
    )


def _document(text: str | None = None, content: str | None = None) -> Document:
    meta = {}
    if text is not None:
        meta["text"] = text
    if content is not None:
        meta["content"] = content
    return Document(id="doc-1", collection_id="kb-1", name="d", meta=meta)


class TestChunkText:
    def test_empty_returns_no_chunks(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_short_text_is_one_chunk(self):
        assert chunk_text("hello world") == ["hello world"]

    def test_paragraphs_pack_greedily(self):
        text = "\n\n".join(["a" * 800, "b" * 800, "c" * 800])
        chunks = chunk_text(text)
        # 800 + 2 + 800 = 1602 > 1500, so each 800-char paragraph is its
        # own chunk.
        assert len(chunks) == 3

    def test_overlong_paragraph_is_hard_split(self):
        text = "x" * 5000
        chunks = chunk_text(text)
        assert len(chunks) >= 2
        assert all(len(c) <= 1500 for c in chunks)


class _Emb:
    def __init__(self, dim: int = 3):
        self._dim = dim

    async def embed(self, *, model, inputs):
        class _R:
            embeddings = [type("V", (), {"vector": [0.1] * self._dim})()]
        return _R()


class _Store:
    def __init__(self):
        self.created = None
        self.puts = []
        self.deleted = []

    async def delete(self, cid, did):
        self.deleted.append((cid, did))

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        self.created = (cid, dimensions)

    async def put(self, record):
        self.puts.append(record)


class TestIndexDocument:
    @pytest.mark.asyncio
    async def test_indexes_chunks_with_embeddings(self):
        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb(dim=4))
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        # Two 800-char paragraphs exceed the 1500-char target when packed
        # together, so they become two chunks.
        n = await index_document(
            document=_document(text="\n\n".join(["a" * 800, "b" * 800])),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 2
        assert store.created == ("kb-1", 4)
        assert len(store.puts) == 2
        assert store.puts[0].document_id == "doc-1"
        assert store.puts[0].chunk_id == "0"
        assert store.puts[1].chunk_id == "1"
        assert len(store.puts[0].vector) == 4
        # Re-index clears old chunks first.
        assert ("kb-1", "doc-1") in store.deleted

    @pytest.mark.asyncio
    async def test_system_collection_skipped(self):
        reg = AsyncMock()
        ssr = AsyncMock()
        n = await index_document(
            document=_document(text="anything"),
            collection=_collection(system=True),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 0
        ssr.get_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_key_fallback(self):
        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        n = await index_document(
            document=_document(content="from content key"),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 1
        assert store.puts[0].text == "from content key"

    @pytest.mark.asyncio
    async def test_empty_document_clears_but_indexes_nothing(self):
        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        n = await index_document(
            document=_document(text=""),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 0
        assert store.created is None  # no chunks, no registration
        assert ("kb-1", "doc-1") in store.deleted  # old chunks cleared
