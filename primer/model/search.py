"""Per-collection search configuration models.

A :class:`Collection` declares an optional :class:`CollectionSearch`
that toggles two retrieval-augmentation techniques on top of the
base vector search:

* :class:`MmrConfig` — Maximal Marginal Relevance. Diversifies
  results so near-duplicate chunks don't all surface together.
  Carbonell & Goldstein, 1998:
  ``MMR = argmax_d [ λ·sim(d, q) − (1−λ)·max sim(d, d_j) ]``.
* :class:`CollectionCrossEncoder` — pointer to a configured
  :class:`primer.model.provider.CrossEncoderProvider` plus tuning
  knobs. When set, retrieved candidates are re-scored by the
  cross-encoder and re-sorted before being returned.

Both fields are optional. Setting neither preserves today's
behaviour (raw vector search). Setting both runs the standard
``vector → cross-encoder rerank → MMR`` pipeline; see
``docs/superpowers/specs/2026-05-05-mmr-cross-encoder-reranking-design.md``
for the surrounding design.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, PositiveInt


class MmrConfig(BaseModel):
    """Maximal Marginal Relevance tuning.

    Cheap (pure linear algebra over the candidate pool) and adds
    essentially zero latency for ``fetch_k < 200``. The diversity
    decision uses the candidate vectors already returned by the
    vector store -- no re-embedding cost.
    """

    lambda_mult: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Diversity↔relevance trade-off. ``1.0`` = pure relevance "
            "(equivalent to vanilla similarity); ``0.0`` = pure "
            "diversity. The conventional default of ``0.5`` matches "
            "LangChain / Haystack / OpenSearch."
        ),
    )
    fetch_k: PositiveInt | None = Field(
        default=None,
        description=(
            "Candidates pulled from the vector store before MMR runs. "
            "``None`` defers the decision to call time; the searcher "
            "uses ``max(50, 10 * k)`` as the default overfetch "
            "(matches LangChain ``fetch_k`` and OpenSearch "
            "``candidates`` defaults)."
        ),
    )


class CollectionCrossEncoder(BaseModel):
    """User-facing pointer to a :class:`CrossEncoderProvider`.

    Mirrors :class:`primer.model.collection.CollectionEmbedder`: the
    ``provider_id`` references an entry in the application's
    :class:`CrossEncoderProvider` registry, and ``model`` names one
    of that provider's permitted models. Both are validated against
    the configured providers at runtime, not here.
    """

    provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the CrossEncoderProvider configured for "
            "this collection. Must match a CrossEncoderProvider.id "
            "in the application's provider registry."
        ),
    )
    model: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side cross-encoder model name "
            "(e.g. ``BAAI/bge-reranker-v2-m3``). Must be one of the "
            "models permitted on the referenced provider."
        ),
    )
    top_n: PositiveInt = Field(
        default=100,
        description=(
            "How many vector-search candidates the cross-encoder "
            "scores. Quality plateaus past ~100 in published "
            "benchmarks; latency grows roughly linearly past that "
            "point. The searcher overfetches from the vector store "
            "to fill this pool."
        ),
    )
    batch_size: PositiveInt = Field(
        default=32,
        description=(
            "Batch size handed to the underlying cross-encoder "
            "predictor. ``32`` is the sentence-transformers default "
            "and works well on CPU; on GPU, 64–128 is typical."
        ),
    )


class CollectionSearch(BaseModel):
    """Per-collection search-augmentation toggles.

    Both fields are independently optional. The searcher runs them
    in the canonical order ``vector → cross-encoder rerank → MMR``
    when both are set:

    * the cross-encoder needs a relevance-rich pool to re-score,
    * MMR diversifies a small already-relevant pool,

    so reversing the order would have the reranker waste compute on
    diverse-but-irrelevant items.
    """

    mmr: MmrConfig | None = Field(
        default=None,
        description=(
            "Diversification config. ``None`` = MMR disabled "
            "(default vector ranking is preserved)."
        ),
    )
    cer: CollectionCrossEncoder | None = Field(
        default=None,
        description=(
            "Cross-encoder reranker config. ``None`` = reranking "
            "disabled (vector-store score is preserved)."
        ),
    )


__all__ = [
    "CollectionCrossEncoder",
    "CollectionSearch",
    "MmrConfig",
]
