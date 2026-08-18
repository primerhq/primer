"""Collection and Document data models.

A *collection* is a set of documents searchable by similarity, semantic,
or hybrid search. The collection declares which embedding provider and
model are used to vectorise its documents; documents reference their
collection by id and carry free-form metadata.

These types are configuration shapes only -- no storage backend, no
search semantics, and no vectorisation pipeline ship in this module.
Those concerns are handled by separate adapters (added in later
sub-projects) that read these models and turn them into operations
against a vector index.
"""

from __future__ import annotations

import re as _re
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

from primer.model.common import Describeable, Identifiable
from primer.model.search import CollectionCrossEncoder


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_SLUG_RE = _re.compile(r"[a-z0-9._-]+")


class CollectionEmbedder(BaseModel):
    """Which embedding provider + model a collection uses to vectorise documents.

    The ``provider_id`` references an :class:`primer.model.provider.EmbeddingProvider`
    by its user-chosen id; the ``model`` names one of that provider's
    permitted embedding models. Both are validated against the application's
    configured providers at runtime, not here -- this model just carries
    the reference.
    """

    provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the EmbeddingProvider configured for this "
            "collection. Must match an EmbeddingProvider.id in the "
            "application's provider registry."
        ),
    )
    model: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side embedding model name to use for this "
            "collection (e.g. 'text-embedding-3-small'). Must be one "
            "of the models permitted on the referenced provider."
        ),
    )


class ChunkingConfig(BaseModel):
    """Chunk sizing for the markdown heading-aware splitter (section 6)."""

    max_chars: int = Field(
        default=1500, ge=200, le=20000,
        description="Target maximum characters per chunk.",
    )
    overlap: int = Field(
        default=200, ge=0, le=2000,
        description="Tail characters carried over between adjacent chunks.",
    )


class CollectionSearchConfig(BaseModel):
    """Per-collection semantic-search opt-in block. None on the parent
    Collection means grep-only (the default)."""

    embedder: CollectionEmbedder = Field(
        ..., description="Embedding provider + model for this collection.",
    )
    vector_store_provider_id: str = Field(
        ..., min_length=1,
        description="Id of the SemanticSearchProvider holding the vectors.",
    )
    cross_encoder: CollectionCrossEncoder | None = Field(
        default=None,
        description="Optional cross-encoder rerank provider + model.",
    )
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    state: Literal["ready", "indexing", "error"] = Field(default="indexing")
    error: str | None = Field(
        default=None, description="Human-readable hint when state == 'error'.",
    )


class Collection(Describeable):
    """A wiki of pure-text documents; optionally vectorised via `search`."""

    _id_prefix: ClassVar[str] = "collection"

    search: CollectionSearchConfig | None = Field(
        default=None,
        description="Semantic-search opt-in. None = grep-only.",
    )
    system: bool = Field(
        default=False,
        description="System-owned; read-only to users through every path.",
    )
    harness_id: str | None = Field(
        default=None,
        description="When set, this row is managed by the named harness.",
    )


class Document(Identifiable):
    """A single document stored in a :class:`Collection`.

    Inherits ``id`` from :class:`Identifiable`. ``collection_id`` is the
    id of the parent :class:`Collection`; ``name`` is a human-readable
    label (distinct from ``id``, which is the wire identifier);
    ``meta`` is a free-form bag the application can use for filtering,
    routing, or display.

    The document's payload (the actual text being indexed) is not on
    this model -- payload storage is the storage backend's concern, and
    different backends model it differently (raw bytes, pre-chunked
    spans, external URI, etc.).
    """

    _id_prefix: ClassVar[str] = "document"

    collection_id: str = Field(..., min_length=1)
    parent_id: str | None = Field(
        default=None, description="Parent document id; None = collection root.",
    )
    slug: str = Field(
        ..., min_length=1,
        description="Path segment under the parent. Strict charset [a-z0-9-] "
        "is enforced at the API/tree-service edge; the model accepts "
        "[a-z0-9._-] for transitional and harness-managed rows.",
    )
    title: str | None = Field(
        default=None, description="Display title; defaults to the slug when unset.",
    )
    path: str = Field(
        ..., min_length=1,
        description="Derived slug-chain mirror maintained by the tree service.",
    )
    meta: dict[str, Any] = Field(default_factory=dict)
    harness_id: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.fullmatch(v):
            raise ValueError("slug must match [a-z0-9._-]+")
        return v

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if v.startswith("/") or v.endswith("/"):
            raise ValueError("path must not start or end with '/'")
        if "\\" in v:
            raise ValueError("path must not contain a backslash")
        if any(ord(ch) < 0x20 for ch in v):
            raise ValueError("path must not contain ASCII control characters")
        for seg in v.split("/"):
            if seg in ("", ".", ".."):
                raise ValueError("path must not contain empty, '.', or '..' segments")
            if seg != seg.strip() or seg.strip() == "":
                raise ValueError(
                    "path segments must not be empty or have leading/trailing "
                    "whitespace"
                )
        return v
