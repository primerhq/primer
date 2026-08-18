"""End-to-end search orchestrator for one :class:`Collection`.

The :class:`CollectionSearcher` ties together the three handles a
real RAG query needs — :class:`Embedder` (to vectorise the query),
:class:`VectorStore` (to retrieve candidates), and optionally
:class:`CrossEncoder` (to rerank) - and runs the
:attr:`Collection.search` config on top of the result.

Pipeline:

1. Resolve fetch size ``N``. No cross-encoder → ``N = k``; with one →
   ``N = max(k, cer.top_n)``.
2. Embed the query.
3. Retrieve ``N`` candidates from the vector store.
4. Rerank (if configured): score each candidate's text against the
   query, replace ``SearchResult.score`` with the cross-encoder
   logit, re-sort descending.
5. Return the top-``k`` hits.

MMR was dropped in S2: its config was unreachable once the Collection
moved to :class:`CollectionSearchConfig`.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from primer.model.chat import TextPart
from primer.model.except_ import BadRequestError, ConfigError
from primer.model.vector import SearchResult, Vector


if TYPE_CHECKING:
    from primer.int.cross_encoder import CrossEncoder
    from primer.int.embedder import Embedder
    from primer.int.vector_store import VectorStore
    from primer.model.collection import Collection
    from primer.model.search import CollectionCrossEncoder


logger = logging.getLogger(__name__)


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
        embedding_model: str | None = None,
    ) -> None:
        search_cfg = collection.search
        if (
            search_cfg is not None
            and search_cfg.cross_encoder is not None
            and cross_encoder is None
        ):
            raise ConfigError(
                f"collection {collection.id!r} configures cross-encoder "
                "reranking but no CrossEncoder was supplied to "
                "CollectionSearcher; pass `cross_encoder=` or remove "
                "`collection.search.cross_encoder`"
            )
        # The model normally comes from the collection's search block.
        # System collections (the semantic catalog, internal collections)
        # carry no such block and pass their configured model explicitly.
        if embedding_model is None:
            if search_cfg is None:
                raise ConfigError(
                    f"collection {collection.id!r} has no search block; pass "
                    "embedding_model= to search it"
                )
            embedding_model = search_cfg.embedder.model
        self._embedding_model = embedding_model
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
        cer_cfg = search_cfg.cross_encoder if search_cfg is not None else None

        # Stage 0: resolve fetch size N.
        n = self._resolve_fetch_size(k=k, cer=cer_cfg)

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

        # Stage 4: trim to top-k.
        return candidates[:k]

    # ---- Stages -----------------------------------------------------------

    @staticmethod
    def _resolve_fetch_size(
        *,
        k: int,
        cer: "CollectionCrossEncoder | None",
    ) -> int:
        if cer is None:
            return k
        return max(k, cer.top_n)

    async def _embed_query(self, query: str) -> Vector:
        response = await self._embedder.embed(
            model=self._embedding_model,
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
