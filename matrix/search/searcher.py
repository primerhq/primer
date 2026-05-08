"""End-to-end search orchestrator for one :class:`Collection`.

The :class:`CollectionSearcher` ties together the three handles a
real RAG query needs — :class:`Embedder` (to vectorise the query),
:class:`VectorStore` (to retrieve candidates), and optionally
:class:`CrossEncoder` (to rerank) — and runs the
:attr:`Collection.search` config on top of the result.

Pipeline (see ``docs/superpowers/specs/2026-05-05-mmr-cross-encoder-reranking-design.md``):

1. Resolve fetch size ``N`` from the configured techniques. No
   config → ``N = k``. Only CER → ``N = cer.top_n``. Only MMR →
   ``N = mmr.fetch_k or max(50, 10*k)``. Both → the larger of the
   two so a single retrieval feeds both stages.
2. Embed the query.
3. Retrieve ``N`` candidates from the vector store.
4. CER (if configured): score each candidate's text against the
   query, replace ``SearchResult.score`` with the cross-encoder
   logit, re-sort descending.
5. MMR (if configured): run the standard
   ``λ·sim(d, q) − (1−λ)·max sim(d, d_j)`` loop over the post-rerank
   candidates using their vectors and the query vector.
6. Return the top-``k`` hits.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from matrix.model.chat import TextPart
from matrix.model.except_ import BadRequestError, ConfigError
from matrix.model.vector import SearchResult, Vector


if TYPE_CHECKING:
    from matrix.int.cross_encoder import CrossEncoder
    from matrix.int.embedder import Embedder
    from matrix.int.vector_store import VectorStore
    from matrix.model.collection import Collection
    from matrix.model.search import (
        CollectionCrossEncoder,
        MmrConfig,
    )


logger = logging.getLogger(__name__)


def _default_fetch_k(k: int) -> int:
    """LangChain / OpenSearch convention: ``max(50, 10 * k)``."""
    return max(50, 10 * k)


class CollectionSearcher:
    """End-to-end semantic-search orchestrator for one :class:`Collection`.

    Constructed against live :class:`Embedder` / :class:`VectorStore`
    handles plus an optional :class:`CrossEncoder`. The cross-encoder
    is required iff ``collection.search.cer`` is set; this is checked
    eagerly so misconfiguration surfaces at construction, not on the
    first query.
    """

    def __init__(
        self,
        *,
        collection: "Collection",
        embedder: "Embedder",
        vector_store: "VectorStore",
        cross_encoder: "CrossEncoder | None" = None,
    ) -> None:
        search_cfg = collection.search
        if (
            search_cfg is not None
            and search_cfg.cer is not None
            and cross_encoder is None
        ):
            raise ConfigError(
                f"collection {collection.id!r} configures cross-encoder "
                "reranking but no CrossEncoder was supplied to "
                "CollectionSearcher; pass `cross_encoder=` or remove "
                "`collection.search.cer`"
            )
        self._collection = collection
        self._embedder = embedder
        self._vector_store = vector_store
        self._cross_encoder = cross_encoder

    @property
    def collection(self) -> "Collection":
        return self._collection

    async def search(self, query: str, k: int) -> list[SearchResult]:
        """Run the full search pipeline; return up to ``k`` hits, most relevant first."""
        if k <= 0:
            raise BadRequestError(f"k must be > 0, got {k!r}")
        if not query:
            raise BadRequestError("query must be non-empty")

        search_cfg = self._collection.search
        mmr_cfg = search_cfg.mmr if search_cfg is not None else None
        cer_cfg = search_cfg.cer if search_cfg is not None else None

        # Stage 0: resolve fetch size N.
        n = self._resolve_fetch_size(k=k, mmr=mmr_cfg, cer=cer_cfg)

        # Stage 1: embed the query.
        query_vec = await self._embed_query(query)

        # Stage 2: retrieve candidates.
        candidates = await self._vector_store.search(
            self._collection.id, query_vec, n
        )
        if not candidates:
            return []

        # Stage 3: cross-encoder rerank (if configured).
        if cer_cfg is not None:
            candidates = await self._rerank(query, candidates, cer_cfg)

        # Stage 4: MMR (if configured).
        if mmr_cfg is not None:
            candidates = self._mmr_select(
                query_vec=query_vec,
                candidates=candidates,
                k=k,
                lambda_mult=mmr_cfg.lambda_mult,
            )

        # Stage 5: trim to top-k.
        return candidates[:k]

    # ---- Stages -----------------------------------------------------------

    @staticmethod
    def _resolve_fetch_size(
        *,
        k: int,
        mmr: "MmrConfig | None",
        cer: "CollectionCrossEncoder | None",
    ) -> int:
        if mmr is None and cer is None:
            return k
        cer_n = cer.top_n if cer is not None else 0
        mmr_n = (
            (mmr.fetch_k if mmr.fetch_k is not None else _default_fetch_k(k))
            if mmr is not None
            else 0
        )
        return max(k, cer_n, mmr_n)

    async def _embed_query(self, query: str) -> Vector:
        response = await self._embedder.embed(
            model=self._collection.embedder.model,
            inputs=[TextPart(text=query)],
        )
        if not response.embeddings:
            raise BadRequestError("embedder returned no embedding for the query")
        return list(response.embeddings[0].vector)

    async def _rerank(
        self,
        query: str,
        candidates: list[SearchResult],
        cer: "CollectionCrossEncoder",
    ) -> list[SearchResult]:
        # Trim before scoring so the cross-encoder only sees ``top_n``
        # candidates even if the vector store returned more (e.g. when
        # MMR's larger fetch_k drove the retrieval).
        pool = candidates[: cer.top_n]
        documents = [hit.record.text for hit in pool]
        assert self._cross_encoder is not None  # invariant from __init__
        scores = await self._cross_encoder.score(
            model=cer.model,
            query=query,
            documents=documents,
            batch_size=cer.batch_size,
        )
        if len(scores) != len(pool):
            raise ConfigError(
                f"cross-encoder returned {len(scores)} scores for "
                f"{len(pool)} candidates"
            )
        rescored = [
            SearchResult(record=hit.record, score=float(score))
            for hit, score in zip(pool, scores, strict=True)
        ]
        rescored.sort(key=lambda h: h.score or float("-inf"), reverse=True)
        return rescored

    @staticmethod
    def _mmr_select(
        *,
        query_vec: Vector,
        candidates: list[SearchResult],
        k: int,
        lambda_mult: float,
    ) -> list[SearchResult]:
        # Validate and normalise vectors once. Candidates without a
        # vector cannot participate in MMR — surface a typed error.
        if not query_vec or _norm(query_vec) == 0.0:
            raise ConfigError("MMR: query vector is empty or zero-length")
        candidate_vecs: list[Vector] = []
        for hit in candidates:
            v = hit.record.vector
            if not v:
                raise ConfigError(
                    f"MMR: candidate {hit.record.chunk_id!r} from document "
                    f"{hit.record.document_id!r} has no vector; "
                    "VectorStore.search must populate SearchResult.record.vector "
                    "when MMR is configured"
                )
            if _norm(v) == 0.0:
                raise ConfigError(
                    f"MMR: candidate {hit.record.chunk_id!r} has a "
                    "zero-magnitude vector"
                )
            candidate_vecs.append(v)

        q_unit = _l2_normalise(query_vec)
        cand_units = [_l2_normalise(v) for v in candidate_vecs]
        sim_to_query = [_dot(q_unit, c) for c in cand_units]

        # Iteratively pick the candidate maximising the MMR score.
        selected: list[int] = []
        remaining = list(range(len(candidates)))
        target = min(k, len(candidates))

        while remaining and len(selected) < target:
            best_idx = -1
            best_score = -math.inf
            for i in remaining:
                if not selected:
                    score = sim_to_query[i]
                else:
                    max_sim_to_selected = max(
                        _dot(cand_units[i], cand_units[j]) for j in selected
                    )
                    score = (
                        lambda_mult * sim_to_query[i]
                        - (1.0 - lambda_mult) * max_sim_to_selected
                    )
                if score > best_score:
                    best_score = score
                    best_idx = i
            selected.append(best_idx)
            remaining.remove(best_idx)

        return [candidates[i] for i in selected]


# ---- Vector helpers --------------------------------------------------------


def _norm(v: Vector) -> float:
    return math.sqrt(sum(x * x for x in v))


def _l2_normalise(v: Vector) -> Vector:
    n = _norm(v)
    if n == 0.0:
        return list(v)  # caller checked; this branch is defence-in-depth
    return [x / n for x in v]


def _dot(a: Vector, b: Vector) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


__all__ = ["CollectionSearcher"]
