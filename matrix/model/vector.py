"""Data types for the vector-store layer.

A *vector store* holds embedding vectors generated from chunks of
documents. Each :class:`EmbeddingRecord` is keyed by the composite
``(collection_id, document_id, chunk_id)`` -- one collection has many
documents; one document is split into many chunks; each chunk
contributes one embedding row.

Vector storage is intentionally separate from the
:class:`matrix.model.collection.Collection` /
:class:`matrix.model.collection.Document` configuration models: the
collection / document tables describe *what* is being indexed; the
vector table holds the *embeddings* derived from them. The two layers
live in different stores and may use different backends.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ===========================================================================
# Embedding record
# ===========================================================================


# Vectors are exchanged across the API as plain lists of floats. The
# common-denominator type is the easiest to (de)serialise and lets every
# backend convert to its native representation (NumPy, Arrow, etc.) on
# the way in. List length is not bounded here -- backends typically
# enforce a per-collection dimensionality at insert time.
Vector = list[float]


class EmbeddingRecord(BaseModel):
    """One stored embedding row.

    The composite key is ``(collection_id, document_id, chunk_id)``;
    backends MUST treat that triple as a uniqueness constraint and use
    it as the upsert key for :meth:`matrix.int.VectorStore.put`.

    There is no synthetic ``id`` field because the natural key is
    composite; a single string id would either lose information (just
    ``chunk_id`` is not unique across documents) or have to be derived
    from concatenation (loses the structure callers actually want to
    query). Backends that need a single-column primary key derive one
    internally.
    """

    collection_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the parent Collection.",
    )
    document_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the parent Document within the collection.",
    )
    chunk_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the chunk within the document. Application-defined; "
            "may be a sequence number, a hash, or a logical name."
        ),
    )
    text: str = Field(
        ...,
        description=(
            "The text the embedding was generated from. Stored alongside "
            "the vector so search results can return the original snippet "
            "without a second round-trip to a text store."
        ),
    )
    vector: Vector = Field(
        ...,
        description=(
            "The embedding vector. Length must match the collection's "
            "configured embedding model dimensionality (the vector store "
            "is not aware of that constraint -- backends enforce or the "
            "caller does)."
        ),
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form metadata. Useful for downstream filtering, "
            "audit, and result decoration. Schema is application-defined."
        ),
    )


# ===========================================================================
# Search result
# ===========================================================================


class SearchResult(BaseModel):
    """One hit returned by :meth:`matrix.int.VectorStore.search`.

    Wraps the matched :class:`EmbeddingRecord` with an optional
    similarity ``score``. Higher scores indicate stronger matches
    regardless of the backend's native metric -- backends that compute
    distance (lower-is-better) MUST invert / transform to similarity
    before returning, so callers see a single, consistent convention.
    """

    record: EmbeddingRecord = Field(
        ...,
        description="The matched embedding record.",
    )
    score: float | None = Field(
        default=None,
        description=(
            "Similarity score (higher = more similar). None if the "
            "backend cannot supply one. Scale is backend-specific; "
            "do not compare scores across stores."
        ),
    )


# ===========================================================================
# Vector store config (single-row)
# ===========================================================================


from typing import Literal  # noqa: E402

from matrix.model.common import Identifiable  # noqa: E402


_VectorStoreBackend = Literal["pgvector", "pgvectorscale"]


class VectorStoreConfig(Identifiable):
    """Single-row "active vector store" configuration.

    Stored under the conventional id ``"_active_vector_store"`` in the
    application's :class:`Storage[VectorStoreConfig]`. The
    :class:`matrix.api.registries.VectorStoreRegistry` reads this row
    on first :meth:`get` to discover which backend to construct.

    Phase 3 of the REST API rollout ships CRUD endpoints over this
    model; Phase 0 ships the model only so the registry can read from
    storage as soon as a row exists.
    """

    backend: _VectorStoreBackend = Field(
        ...,
        description=(
            "Backend implementation. Must match a known concrete "
            "``VectorStoreBackend`` (pgvector / pgvectorscale)."
        ),
    )
    settings: dict[str, Any] = Field(
        ...,
        description=(
            "Backend-specific connection / configuration settings. "
            "Free-form because each backend's config shape differs; the "
            "factory in :mod:`matrix.vector.factory` validates the "
            "shape at construction time."
        ),
    )


__all__ = [
    "EmbeddingRecord",
    "SearchResult",
    "Vector",
    "VectorStoreConfig",
]
