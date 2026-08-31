"""Unit tests for primer.search.searcher.CollectionSearcher."""

from __future__ import annotations

from typing import Any

import pytest

from primer.model.collection import (
    Collection,
    CollectionEmbedder,
    CollectionSearchConfig,
)
from primer.model.embedding import EmbedResponse, Embedding
from primer.model.except_ import BadRequestError, ConfigError
from primer.model.search import (
    CollectionCrossEncoder,
)
from primer.model.vector import EmbeddingRecord, SearchResult, Vector
from primer.search.searcher import CollectionSearcher


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeEmbedder:
    """Returns a fixed query vector regardless of input."""

    def __init__(self, vector: Vector) -> None:
        self._vector = vector
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    async def embed(self, *, model: str, inputs, **kwargs):
        self.calls.append({"model": model, "inputs": inputs, **kwargs})
        return EmbedResponse(
            model=model,
            embeddings=[Embedding(index=0, vector=list(self._vector))],
            usage=None,
        )


class _FakeVectorStore:
    """Returns a scripted candidate list; records (collection_id, vector, k)."""

    def __init__(self, candidates: list[SearchResult]) -> None:
        self._candidates = candidates
        self.calls: list[dict[str, Any]] = []

    async def create_collection(self, *args, **kwargs):
        pass

    async def put(self, *args, **kwargs):
        pass

    async def search(self, collection_id, vector, k):
        self.calls.append(
            {"collection_id": collection_id, "vector": list(vector), "k": k}
        )
        # Backends respect ``k`` by trimming; emulate that.
        return list(self._candidates[:k])

    async def search_by_meta(self, *args, **kwargs):
        return []

    async def get(self, *args, **kwargs):
        return []

    async def delete(self, *args, **kwargs):
        pass


class _FakeCrossEncoder:
    """Returns a scripted score list, in input order."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["ce"]

    async def score(self, *, model, query, documents, batch_size=32):
        self.calls.append(
            {
                "model": model,
                "query": query,
                "documents": list(documents),
                "batch_size": batch_size,
            }
        )
        # Honor input length (the searcher trims to top_n before calling).
        return list(self._scores[: len(documents)])


# ===========================================================================
# Helpers
# ===========================================================================


def _hit(
    chunk_id: str,
    *,
    text: str,
    vector: Vector,
    score: float | None = None,
) -> SearchResult:
    return SearchResult(
        record=EmbeddingRecord(
            collection_id="c1",
            document_id="d1",
            chunk_id=chunk_id,
            text=text,
            vector=list(vector),
            meta={},
        ),
        score=score,
    )


def _collection(
    *, cross_encoder: CollectionCrossEncoder | None = None
) -> Collection:
    return Collection(
        id="c1",
        description="t",
        search=CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="p", model="m"),
            vector_store_provider_id="ssp-test",
            cross_encoder=cross_encoder,
        ),
    )


# ===========================================================================
# Construction guards
# ===========================================================================


class TestConstruction:
    def test_no_search_config_works_without_cross_encoder(self) -> None:
        coll = _collection()
        # Should not raise.
        CollectionSearcher(
            collection=coll,
            embedder=_FakeEmbedder([1.0]),
            vector_store=_FakeVectorStore([]),
        )

    def test_cer_config_without_cross_encoder_raises(self) -> None:
        coll = _collection(
            cross_encoder=CollectionCrossEncoder(provider_id="p", model="m"),
        )
        with pytest.raises(ConfigError, match="cross-encoder"):
            CollectionSearcher(
                collection=coll,
                embedder=_FakeEmbedder([1.0]),
                vector_store=_FakeVectorStore([]),
                cross_encoder=None,
            )

# ===========================================================================
# Pre-pipeline validation
# ===========================================================================


class TestArgValidation:
    @pytest.mark.asyncio
    async def test_k_zero_raises(self) -> None:
        searcher = CollectionSearcher(
            collection=_collection(),
            embedder=_FakeEmbedder([1.0]),
            vector_store=_FakeVectorStore([]),
        )
        with pytest.raises(BadRequestError, match="k must be > 0"):
            await searcher.search("q", 0)

    @pytest.mark.asyncio
    async def test_empty_query_raises(self) -> None:
        searcher = CollectionSearcher(
            collection=_collection(),
            embedder=_FakeEmbedder([1.0]),
            vector_store=_FakeVectorStore([]),
        )
        with pytest.raises(BadRequestError, match="query"):
            await searcher.search("", 5)


# ===========================================================================
# No search config → vanilla pass-through
# ===========================================================================


class TestVanillaPassthrough:
    @pytest.mark.asyncio
    async def test_no_search_config_passes_top_k_unchanged(self) -> None:
        cands = [
            _hit("c1", text="a", vector=[1.0, 0.0], score=0.9),
            _hit("c2", text="b", vector=[0.9, 0.1], score=0.8),
            _hit("c3", text="c", vector=[0.0, 1.0], score=0.7),
        ]
        store = _FakeVectorStore(cands)
        searcher = CollectionSearcher(
            collection=_collection(),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        out = await searcher.search("q", k=2)
        # Vector store called with N=k.
        assert store.calls[0]["k"] == 2
        # Result preserves order and score.
        assert [h.record.chunk_id for h in out] == ["c1", "c2"]
        assert [h.score for h in out] == [0.9, 0.8]

    @pytest.mark.asyncio
    async def test_empty_vector_store_result_returns_empty(self) -> None:
        store = _FakeVectorStore([])
        searcher = CollectionSearcher(
            collection=_collection(),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        out = await searcher.search("q", k=5)
        assert out == []


# ===========================================================================
# Cross-encoder rerank
# ===========================================================================


class TestCrossEncoderRerank:
    @pytest.mark.asyncio
    async def test_cer_replaces_score_and_resorts(self) -> None:
        cands = [
            _hit(
                "low-vec-but-high-ce",
                text="paris is the capital",
                vector=[1.0, 0.0],
                score=0.1,
            ),
            _hit(
                "high-vec-but-low-ce",
                text="berlin is in germany",
                vector=[0.9, 0.0],
                score=0.9,
            ),
        ]
        store = _FakeVectorStore(cands)
        ce = _FakeCrossEncoder([5.0, 0.5])  # first doc wins under CER

        searcher = CollectionSearcher(
            collection=_collection(
                cross_encoder=CollectionCrossEncoder(
                    provider_id="p", model="rerank-m", top_n=10, batch_size=4,
                ),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
            cross_encoder=ce,
        )
        out = await searcher.search("capital of france", k=2)

        # Order is now CER-sorted, scores replaced with CE logits.
        assert [h.record.chunk_id for h in out] == [
            "low-vec-but-high-ce",
            "high-vec-but-low-ce",
        ]
        assert [h.score for h in out] == [5.0, 0.5]
        # CE was called once with (query, doc) batches.
        assert len(ce.calls) == 1
        assert ce.calls[0]["model"] == "rerank-m"
        assert ce.calls[0]["batch_size"] == 4

    @pytest.mark.asyncio
    async def test_cer_overfetch_drives_vector_store_k(self) -> None:
        store = _FakeVectorStore([])
        ce = _FakeCrossEncoder([])
        searcher = CollectionSearcher(
            collection=_collection(
                cross_encoder=CollectionCrossEncoder(
                    provider_id="p", model="m", top_n=75
                ),
            ),
            embedder=_FakeEmbedder([1.0]),
            vector_store=store,
            cross_encoder=ce,
        )
        await searcher.search("q", k=3)
        # N = max(k, top_n) = 75.
        assert store.calls[0]["k"] == 75


# ===========================================================================
# MMR
# ===========================================================================


# ===========================================================================
# Both MMR + CER together
# ===========================================================================


