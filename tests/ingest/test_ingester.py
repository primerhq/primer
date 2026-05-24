"""Tests for matrix.ingest.ingester.DocumentIngester."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import pytest

from matrix.ingest.ingester import DocumentIngester
from matrix.ingest.loader import DocumentLoader
from matrix.ingest.splitters.recursive import RecursiveSplitter
from matrix.model.collection import Collection, CollectionEmbedder, Document
from matrix.model.embedding import Embedding, EmbedResponse
from matrix.model.except_ import BadRequestError
from matrix.model.ingest import LoadedDocument
from matrix.model.vector import EmbeddingRecord, SearchResult, Vector


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeLoader(DocumentLoader):
    """Returns a fixed :class:`LoadedDocument`."""

    def __init__(self, text: str, *, meta: dict[str, Any] | None = None) -> None:
        self._text = text
        self._meta = dict(meta or {})

    async def load(self, source: bytes | Path | str) -> LoadedDocument:
        return LoadedDocument(text=self._text, meta=self._meta)


class _FakeEmbedder:
    """Stub :class:`Embedder` that returns deterministic synthetic vectors."""

    def __init__(self, *, dimensions: int = 4) -> None:
        self._dimensions = dimensions
        self.calls: list[dict[str, Any]] = []

    async def list_models(self) -> Iterable[str]:
        return ["fake-model"]

    async def embed(
        self,
        *,
        model: str,
        inputs: list,
        output_dimensions: int | None = None,
        config=None,
    ) -> EmbedResponse:
        self.calls.append({"model": model, "n": len(inputs)})
        embeddings = []
        for i in range(len(inputs)):
            vec: Vector = [float(i + 1)] + [0.0] * (self._dimensions - 1)
            embeddings.append(Embedding(index=i, vector=vec))
        return EmbedResponse(model=model, embeddings=embeddings)


class _InMemoryVectorStore:
    """Bare-minimum :class:`VectorStore` test double."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.records: dict[tuple[str, str, str], EmbeddingRecord] = {}
        self.deleted_docs: list[tuple[str, str]] = []

    async def create_collection(
        self,
        collection_id: str,
        *,
        dimensions: int,
        distance: Literal["cosine", "l2", "ip"] = "cosine",
    ) -> None:
        existing = self.collections.get(collection_id)
        if existing is not None:
            if existing["dimensions"] != dimensions:
                from matrix.model.except_ import ConflictError

                raise ConflictError(
                    f"existing dim {existing['dimensions']} != new {dimensions}"
                )
            return
        self.collections[collection_id] = {
            "dimensions": dimensions,
            "distance": distance,
        }

    async def put(self, record: EmbeddingRecord) -> None:
        self.records[
            (record.collection_id, record.document_id, record.chunk_id)
        ] = record

    async def search(
        self,
        collection_id: str,
        vector: Vector,
        k: int,
    ) -> list[SearchResult]:
        return []

    async def search_by_meta(
        self,
        collection_id: str,
        meta: dict[str, Any],
    ) -> list[EmbeddingRecord]:
        return [
            r
            for (cid, _, _), r in self.records.items()
            if cid == collection_id
        ]

    async def get(
        self,
        collection_id: str,
        document_id: str,
    ) -> list[EmbeddingRecord]:
        return sorted(
            (
                r
                for (cid, did, _), r in self.records.items()
                if cid == collection_id and did == document_id
            ),
            key=lambda r: r.chunk_id,
        )

    async def delete(
        self,
        collection_id: str,
        document_id: str,
    ) -> None:
        self.deleted_docs.append((collection_id, document_id))
        keys_to_delete = [
            k
            for k in self.records
            if k[0] == collection_id and k[1] == document_id
        ]
        for k in keys_to_delete:
            del self.records[k]


# ===========================================================================
# Helpers
# ===========================================================================


def _collection() -> Collection:
    return Collection(
        id="kb-research",
        description="research kb",
        embedder=CollectionEmbedder(
            provider_id="emb-1",
            model="fake-model",
        ),
        search_provider_id="ssp-test",
    )


def _document(*, collection_id: str = "kb-research") -> Document:
    return Document(
        id="doc-001",
        collection_id=collection_id,
        name="test.pdf",
        meta={"source": "synthetic"},
    )


def _ingester(
    *,
    embedder: _FakeEmbedder,
    vector_store: _InMemoryVectorStore,
    text: str = "sentence one. sentence two. sentence three.",
    loader_meta: dict | None = None,
    chunk_size: int = 30,
    chunk_overlap: int = 0,
    batch_size: int = 32,
) -> DocumentIngester:
    return DocumentIngester(
        collection=_collection(),
        embedder=embedder,  # type: ignore[arg-type]
        vector_store=vector_store,  # type: ignore[arg-type]
        loader=_FakeLoader(text, meta=loader_meta),
        splitter=RecursiveSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        ),
        batch_size=batch_size,
    )


# ===========================================================================
# Constructor validation
# ===========================================================================


class TestConstructor:
    def test_invalid_batch_size(self) -> None:
        emb = _FakeEmbedder()
        vs = _InMemoryVectorStore()
        with pytest.raises(ValueError):
            DocumentIngester(
                collection=_collection(),
                embedder=emb,  # type: ignore[arg-type]
                vector_store=vs,  # type: ignore[arg-type]
                loader=_FakeLoader("x"),
                splitter=RecursiveSplitter(),
                batch_size=0,
            )


# ===========================================================================
# ingest()
# ===========================================================================


class TestIngest:
    @pytest.mark.asyncio
    async def test_collection_id_mismatch_raises(self) -> None:
        emb = _FakeEmbedder()
        vs = _InMemoryVectorStore()
        ing = _ingester(embedder=emb, vector_store=vs)
        bad_doc = _document(collection_id="other-kb")
        with pytest.raises(BadRequestError):
            await ing.ingest(bad_doc, source=b"data")

    @pytest.mark.asyncio
    async def test_empty_document_zero_chunks(self) -> None:
        emb = _FakeEmbedder()
        vs = _InMemoryVectorStore()
        ing = _ingester(embedder=emb, vector_store=vs, text="")
        result = await ing.ingest(_document(), source=b"data")
        assert result.chunks_indexed == 0
        assert result.replaced is False
        assert vs.records == {}
        assert vs.collections == {}

    @pytest.mark.asyncio
    async def test_single_chunk(self) -> None:
        emb = _FakeEmbedder(dimensions=4)
        vs = _InMemoryVectorStore()
        ing = _ingester(
            embedder=emb,
            vector_store=vs,
            text="hello",
            chunk_size=100,
        )
        result = await ing.ingest(_document(), source=b"data")
        assert result.chunks_indexed == 1
        assert result.dimensions == 4
        assert vs.collections["kb-research"]["dimensions"] == 4
        records = await vs.get("kb-research", "doc-001")
        assert len(records) == 1
        assert records[0].chunk_id == "chunk-000000"
        assert len(records[0].vector) == 4

    @pytest.mark.asyncio
    async def test_many_chunks_batched(self) -> None:
        emb = _FakeEmbedder(dimensions=4)
        vs = _InMemoryVectorStore()
        long_text = ". ".join([f"sentence {i}" for i in range(50)])
        ing = _ingester(
            embedder=emb,
            vector_store=vs,
            text=long_text,
            chunk_size=40,
            chunk_overlap=0,
            batch_size=4,
        )
        result = await ing.ingest(_document(), source=b"data")
        assert result.chunks_indexed >= 5
        records = await vs.get("kb-research", "doc-001")
        assert len(records) == result.chunks_indexed
        ids = [r.chunk_id for r in records]
        assert ids == sorted(ids)

    @pytest.mark.asyncio
    async def test_replace_deletes_first(self) -> None:
        emb = _FakeEmbedder(dimensions=4)
        vs = _InMemoryVectorStore()
        ing = _ingester(
            embedder=emb, vector_store=vs, text="hello", chunk_size=100
        )
        await ing.ingest(_document(), source=b"data")
        await ing.ingest(_document(), source=b"data", replace=True)
        assert ("kb-research", "doc-001") in vs.deleted_docs

    @pytest.mark.asyncio
    async def test_loader_meta_propagates_under_source_doc_key(self) -> None:
        emb = _FakeEmbedder(dimensions=4)
        vs = _InMemoryVectorStore()
        ing = _ingester(
            embedder=emb,
            vector_store=vs,
            text="hello",
            loader_meta={"page_count": 7, "lang": "en"},
            chunk_size=100,
        )
        await ing.ingest(_document(), source=b"data")
        records = await vs.get("kb-research", "doc-001")
        assert records[0].meta.get("source_doc") == {
            "page_count": 7,
            "lang": "en",
        }

    @pytest.mark.asyncio
    async def test_bytes_loaded_from_loader_meta(self) -> None:
        emb = _FakeEmbedder(dimensions=4)
        vs = _InMemoryVectorStore()
        ing = _ingester(
            embedder=emb,
            vector_store=vs,
            text="hello",
            loader_meta={"bytes_loaded": 42},
            chunk_size=100,
        )
        result = await ing.ingest(_document(), source=b"data")
        assert result.bytes_loaded == 42
