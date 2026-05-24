"""Unit tests for matrix.search.searcher.CollectionSearcher."""

from __future__ import annotations

from typing import Any

import pytest

from matrix.model.collection import Collection, CollectionEmbedder
from matrix.model.embedding import EmbedResponse, Embedding
from matrix.model.except_ import BadRequestError, ConfigError
from matrix.model.search import (
    CollectionCrossEncoder,
    CollectionSearch,
    MmrConfig,
)
from matrix.model.vector import EmbeddingRecord, SearchResult, Vector
from matrix.search.searcher import CollectionSearcher


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


def _collection(*, search: CollectionSearch | None = None) -> Collection:
    return Collection(
        id="c1",
        description="t",
        embedder=CollectionEmbedder(provider_id="p", model="m"),
        search_provider_id="ssp-test",
        search=search,
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
            search=CollectionSearch(
                cer=CollectionCrossEncoder(provider_id="p", model="m"),
            ),
        )
        with pytest.raises(ConfigError, match="cross-encoder"):
            CollectionSearcher(
                collection=coll,
                embedder=_FakeEmbedder([1.0]),
                vector_store=_FakeVectorStore([]),
                cross_encoder=None,
            )

    def test_mmr_only_does_not_require_cross_encoder(self) -> None:
        coll = _collection(search=CollectionSearch(mmr=MmrConfig()))
        # Should not raise.
        CollectionSearcher(
            collection=coll,
            embedder=_FakeEmbedder([1.0]),
            vector_store=_FakeVectorStore([]),
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
            collection=_collection(search=CollectionSearch(mmr=MmrConfig())),
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
                search=CollectionSearch(
                    cer=CollectionCrossEncoder(
                        provider_id="p",
                        model="rerank-m",
                        top_n=10,
                        batch_size=4,
                    ),
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
                search=CollectionSearch(
                    cer=CollectionCrossEncoder(
                        provider_id="p", model="m", top_n=75
                    ),
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


class TestMmr:
    @pytest.mark.asyncio
    async def test_mmr_overfetch_default_max_50_or_10k(self) -> None:
        store = _FakeVectorStore([])
        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(mmr=MmrConfig()),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        # k=3 → max(50, 30) = 50
        await searcher.search("q", k=3)
        assert store.calls[0]["k"] == 50
        # k=10 → max(50, 100) = 100
        await searcher.search("q", k=10)
        assert store.calls[1]["k"] == 100

    @pytest.mark.asyncio
    async def test_mmr_explicit_fetch_k_honored(self) -> None:
        store = _FakeVectorStore([])
        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(mmr=MmrConfig(fetch_k=8)),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        await searcher.search("q", k=3)
        # k=3 vs explicit fetch_k=8 → 8.
        assert store.calls[0]["k"] == 8

    @pytest.mark.asyncio
    async def test_mmr_lambda_one_equals_relevance_only(self) -> None:
        # With λ=1.0 the diversity term is zero ⇒ ranking by similarity.
        # Build candidates ordered by descending similarity.
        cands = [
            _hit("a", text="x", vector=[1.0, 0.0]),  # highest sim
            _hit("b", text="y", vector=[0.99, 0.01]),
            _hit("c", text="z", vector=[0.0, 1.0]),  # lowest sim
        ]
        store = _FakeVectorStore(cands)
        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(
                    mmr=MmrConfig(lambda_mult=1.0, fetch_k=3),
                ),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        out = await searcher.search("q", k=3)
        assert [h.record.chunk_id for h in out] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_mmr_lambda_zero_first_pick_is_most_relevant(self) -> None:
        # With λ=0.0 the very first selection is still the most-relevant
        # (selected list is empty, so diversity term doesn't apply yet).
        # The second pick will be the most-distant from the first.
        cands = [
            _hit("near1", text="x", vector=[1.0, 0.0]),
            _hit("near2", text="y", vector=[0.99, 0.01]),
            _hit("far", text="z", vector=[0.0, 1.0]),
        ]
        store = _FakeVectorStore(cands)
        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(
                    mmr=MmrConfig(lambda_mult=0.0, fetch_k=3),
                ),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        out = await searcher.search("q", k=2)
        chunk_ids = [h.record.chunk_id for h in out]
        # First pick = most-relevant. Second pick = most-distant from first.
        assert chunk_ids[0] == "near1"
        assert chunk_ids[1] == "far"

    @pytest.mark.asyncio
    async def test_mmr_returns_at_most_k(self) -> None:
        cands = [
            _hit(f"c{i}", text=f"d{i}", vector=[1.0, 0.0]) for i in range(5)
        ]
        store = _FakeVectorStore(cands)
        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(mmr=MmrConfig(fetch_k=5)),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        out = await searcher.search("q", k=3)
        assert len(out) == 3

    @pytest.mark.asyncio
    async def test_mmr_handles_fewer_candidates_than_k(self) -> None:
        cands = [_hit("c0", text="d", vector=[1.0, 0.0])]
        store = _FakeVectorStore(cands)
        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(mmr=MmrConfig(fetch_k=5)),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        out = await searcher.search("q", k=10)
        assert len(out) == 1

    @pytest.mark.asyncio
    async def test_mmr_missing_vector_raises_config_error(self) -> None:
        # Manually construct a SearchResult whose record has no vector.
        # ``model_construct`` skips Pydantic validation so we can inject
        # the empty list directly.
        bad_record = EmbeddingRecord.model_construct(
            collection_id="c1",
            document_id="d1",
            chunk_id="bad",
            text="no vector",
            vector=[],
            meta={},
        )
        store = _FakeVectorStore(
            [SearchResult(record=bad_record, score=None)]
        )
        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(mmr=MmrConfig(fetch_k=1)),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
        )
        with pytest.raises(ConfigError, match="no vector"):
            await searcher.search("q", k=1)


# ===========================================================================
# Both MMR + CER together
# ===========================================================================


class TestBothEnabled:
    @pytest.mark.asyncio
    async def test_overfetch_uses_max_of_both(self) -> None:
        store = _FakeVectorStore([])
        ce = _FakeCrossEncoder([])
        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(
                    mmr=MmrConfig(fetch_k=20),
                    cer=CollectionCrossEncoder(
                        provider_id="p", model="m", top_n=80
                    ),
                ),
            ),
            embedder=_FakeEmbedder([1.0]),
            vector_store=store,
            cross_encoder=ce,
        )
        await searcher.search("q", k=5)
        # max(k=5, top_n=80, fetch_k=20) = 80.
        assert store.calls[0]["k"] == 80

    @pytest.mark.asyncio
    async def test_pipeline_runs_cer_before_mmr(self) -> None:
        # Two clusters of near-duplicate vectors plus one outlier.
        cands = [
            _hit(
                "near_high_ce_1",
                text="paris france",
                vector=[1.0, 0.0],
                score=0.5,
            ),
            _hit(
                "near_high_ce_2",
                text="paris france capital",
                vector=[0.99, 0.01],
                score=0.5,
            ),
            _hit(
                "far_low_ce",
                text="bananas in pyjamas",
                vector=[0.0, 1.0],
                score=0.5,
            ),
        ]
        store = _FakeVectorStore(cands)
        # Cross-encoder ranks: near_high_ce_1=10, near_high_ce_2=8, far_low_ce=1.
        ce = _FakeCrossEncoder([10.0, 8.0, 1.0])

        searcher = CollectionSearcher(
            collection=_collection(
                search=CollectionSearch(
                    cer=CollectionCrossEncoder(
                        provider_id="p", model="m", top_n=3
                    ),
                    mmr=MmrConfig(lambda_mult=0.3, fetch_k=3),
                ),
            ),
            embedder=_FakeEmbedder([1.0, 0.0]),
            vector_store=store,
            cross_encoder=ce,
        )
        out = await searcher.search("q", k=2)
        # CER promoted near_high_ce_1 to position 0.
        # MMR's first pick = the most-relevant item (near_high_ce_1).
        # MMR's second pick at λ=0.3 weights diversity > relevance:
        #   - near_high_ce_2: 0.3*≈1.0 − 0.7*≈1.0 ≈ −0.4 (near-duplicate)
        #   - far_low_ce:     0.3*0.0 − 0.7*0.0 =     0.0 (maximally distant)
        # The diversity penalty makes far_low_ce win cleanly.
        # NB: MMR uses VECTORS for the diversity decision, not CE scores.
        assert out[0].record.chunk_id == "near_high_ce_1"
        assert out[1].record.chunk_id == "far_low_ce"
