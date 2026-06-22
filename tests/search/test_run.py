"""Unit tests for primer.search.run.run_collection_search.

``run_collection_search`` is the single entry point both the REST search
route and the ``system__search_collection`` tool call. It must:

* run a plain ``store.search`` when the collection declares no ``search``
  config -- reusing a caller-supplied query vector so the no-config path
  does not double-embed; and
* run the full :class:`CollectionSearcher` pipeline (resolving the
  configured cross-encoder) when ``search.cer`` / ``search.mmr`` is set,
  so reranking + MMR actually take effect on the live read path.

These pin the regression behind cookbook recipe #8: before this helper,
both call sites bypassed the searcher and the ``search`` config was inert.
"""

from __future__ import annotations

from typing import Any

import pytest

from primer.model.collection import Collection, CollectionEmbedder
from primer.model.embedding import EmbedResponse, Embedding
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.search import (
    CollectionCrossEncoder,
    CollectionSearch,
    MmrConfig,
)
from primer.model.vector import EmbeddingRecord, SearchResult, Vector
from primer.search.run import run_collection_search


# --- Fakes (mirror tests/search/test_searcher.py) --------------------------


class _FakeEmbedder:
    def __init__(self, vector: Vector) -> None:
        self._vector = vector
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    async def embed(self, *, model: str, inputs, **kwargs):
        self.calls.append({"model": model, "inputs": inputs})
        return EmbedResponse(
            model=model,
            embeddings=[Embedding(index=0, vector=list(self._vector))],
            usage=None,
        )


class _FakeVectorStore:
    def __init__(self, candidates: list[SearchResult]) -> None:
        self._candidates = candidates
        self.calls: list[dict[str, Any]] = []

    async def search(self, collection_id, vector, k):
        self.calls.append(
            {"collection_id": collection_id, "vector": list(vector), "k": k}
        )
        return list(self._candidates[:k])


class _FakeCrossEncoder:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["ce"]

    async def score(self, *, model, query, documents, batch_size=32):
        self.calls.append({"model": model, "query": query})
        return list(self._scores[: len(documents)])


class _FakeResolver:
    """Stands in for ProviderRegistry.get_cross_encoder."""

    def __init__(self, cross_encoder: _FakeCrossEncoder | None) -> None:
        self._ce = cross_encoder
        self.calls: list[str] = []

    async def get_cross_encoder(self, provider_id: str):
        self.calls.append(provider_id)
        if self._ce is None:
            raise NotFoundError(f"CrossEncoderProvider {provider_id!r} missing")
        return self._ce


def _hit(chunk_id: str, *, text: str, vector: Vector, score: float | None) -> SearchResult:
    return SearchResult(
        record=EmbeddingRecord(
            collection_id="c1",
            document_id=chunk_id,
            chunk_id=chunk_id,
            text=text,
            vector=list(vector),
            meta={"document_name": f"{chunk_id}.md"},
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


# --- No-config path: plain vector, reuse the supplied query vector ----------


@pytest.mark.asyncio
async def test_no_search_config_runs_plain_vector_search() -> None:
    cands = [
        _hit("a", text="a", vector=[1.0, 0.0], score=0.9),
        _hit("b", text="b", vector=[0.0, 1.0], score=0.5),
    ]
    embedder = _FakeEmbedder([1.0, 0.0])
    store = _FakeVectorStore(cands)
    resolver = _FakeResolver(None)

    out = await run_collection_search(
        collection=_collection(),
        embedder=embedder,
        store=store,
        query="q",
        top_k=2,
        cross_encoder_resolver=resolver,
        query_vector=[0.3, 0.7],
    )

    assert [h.record.chunk_id for h in out] == ["a", "b"]
    # Reused the supplied vector -> did NOT embed again, and passed it through.
    assert embedder.calls == []
    assert store.calls == [{"collection_id": "c1", "vector": [0.3, 0.7], "k": 2}]
    # No rerank configured -> the resolver is never touched.
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_no_search_config_embeds_when_no_vector_supplied() -> None:
    cands = [_hit("a", text="a", vector=[1.0, 0.0], score=0.9)]
    embedder = _FakeEmbedder([1.0, 0.0])
    store = _FakeVectorStore(cands)

    out = await run_collection_search(
        collection=_collection(),
        embedder=embedder,
        store=store,
        query="q",
        top_k=1,
        cross_encoder_resolver=_FakeResolver(None),
    )

    assert [h.record.chunk_id for h in out] == ["a"]
    assert len(embedder.calls) == 1
    assert store.calls[0]["vector"] == [1.0, 0.0]


# --- CER path: cross-encoder reorders + resolver is invoked ----------------


@pytest.mark.asyncio
async def test_cer_config_reranks_and_resolves_cross_encoder() -> None:
    # Vector order is [a, b]; the cross-encoder scores b higher -> flips to [b, a].
    cands = [
        _hit("a", text="a", vector=[1.0, 0.0], score=0.9),
        _hit("b", text="b", vector=[0.0, 1.0], score=0.5),
    ]
    embedder = _FakeEmbedder([1.0, 0.0])
    store = _FakeVectorStore(cands)
    ce = _FakeCrossEncoder([0.1, 0.9])  # a -> 0.1, b -> 0.9
    resolver = _FakeResolver(ce)
    search = CollectionSearch(
        cer=CollectionCrossEncoder(provider_id="ce-1", model="ce", top_n=50)
    )

    out = await run_collection_search(
        collection=_collection(search=search),
        embedder=embedder,
        store=store,
        query="q",
        top_k=2,
        cross_encoder_resolver=resolver,
        query_vector=[1.0, 0.0],
    )

    # Cross-encoder demonstrably reordered the vector ranking.
    assert [h.record.chunk_id for h in out] == ["b", "a"]
    assert [round(h.score, 3) for h in out] == [0.9, 0.1]
    # The configured provider was resolved exactly once.
    assert resolver.calls == ["ce-1"]
    assert len(ce.calls) == 1


@pytest.mark.asyncio
async def test_cer_and_mmr_both_apply() -> None:
    cands = [
        _hit("a", text="a", vector=[1.0, 0.0], score=0.9),
        _hit("b", text="b", vector=[1.0, 0.0], score=0.8),  # near-dup of a
        _hit("c", text="c", vector=[0.0, 1.0], score=0.4),
    ]
    embedder = _FakeEmbedder([1.0, 0.0])
    store = _FakeVectorStore(cands)
    # After rerank a,b stay top; MMR (diversity-weighted) should then prefer the
    # diverse c over the near-duplicate b for the 2nd slot. b is an exact vector
    # duplicate of a, so its diversity penalty sinks it below the orthogonal c.
    ce = _FakeCrossEncoder([0.9, 0.85, 0.3])
    resolver = _FakeResolver(ce)
    search = CollectionSearch(
        cer=CollectionCrossEncoder(provider_id="ce-1", model="ce", top_n=50),
        mmr=MmrConfig(lambda_mult=0.1, fetch_k=50),
    )

    out = await run_collection_search(
        collection=_collection(search=search),
        embedder=embedder,
        store=store,
        query="q",
        top_k=2,
        cross_encoder_resolver=resolver,
        query_vector=[1.0, 0.0],
    )

    ids = [h.record.chunk_id for h in out]
    assert ids[0] == "a"  # top reranked hit
    assert ids[1] == "c"  # MMR diversified away from the near-duplicate b
    assert resolver.calls == ["ce-1"]


@pytest.mark.asyncio
async def test_invalid_top_k_raises() -> None:
    with pytest.raises(BadRequestError):
        await run_collection_search(
            collection=_collection(),
            embedder=_FakeEmbedder([1.0]),
            store=_FakeVectorStore([]),
            query="q",
            top_k=0,
            cross_encoder_resolver=_FakeResolver(None),
        )
