"""Per-collection search configuration models.

A :class:`Collection` declares an optional search block
that toggles two retrieval-augmentation techniques on top of the
base vector search:

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


__all__ = [
    "CollectionCrossEncoder",
]
