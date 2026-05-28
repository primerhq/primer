"""Data types for the document-ingestion pipeline.

The pipeline turns one external document into one or more
:class:`matrix.model.vector.EmbeddingRecord` rows. Three intermediate
models live here:

* :class:`LoadedDocument` -- what a :class:`DocumentLoader` produces:
  the document's text content (typically markdown) plus optional
  loader-specific structural metadata that semantic splitters can
  use.
* :class:`Chunk` -- what a :class:`DocumentSplitter` produces: one
  span of text + its position within the parent document + free-form
  per-chunk metadata.
* :class:`IngestResult` -- what :meth:`DocumentIngester.ingest`
  returns: telemetry + post-conditions for the caller.

See ``docs/superpowers/specs/2026-05-03-document-ingestion-design.md``
for the surrounding design.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoadedDocument(BaseModel):
    """Intermediate representation produced by a :class:`DocumentLoader`."""

    text: str = Field(
        ...,
        description=(
            "The document's text content. Loaders that emit markdown "
            "leave the markdown formatting intact so structure-aware "
            "splitters can use it."
        ),
    )
    mime_type: str | None = Field(
        default=None,
        description=(
            "MIME type of the original source, when known. Used by "
            "splitters that adjust behaviour per format (e.g. avoid "
            "splitting inside code fences)."
        ),
    )
    structure: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Loader-specific structured representation suitable for "
            "structure-aware splitters. ``DoclingLoader`` (the default) "
            "populates this with the serialised ``DoclingDocument`` JSON. "
            "Custom loaders may leave it ``None``; splitters that don't "
            "understand the structure ignore it."
        ),
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form metadata the loader extracted (page count, "
            "language detection, source URL, original filename, etc.). "
            "Propagated onto each chunk's parent embedding record's "
            "``meta`` under the ``source_doc`` key."
        ),
    )


class Chunk(BaseModel):
    """One chunk produced by a :class:`DocumentSplitter`.

    The ingester turns each ``Chunk`` into one
    :class:`matrix.model.vector.EmbeddingRecord` after embedding.
    """

    text: str = Field(
        ...,
        description="The chunk's text content (what gets embedded).",
    )
    position: int = Field(
        ...,
        ge=0,
        description=(
            "Sequence number within the parent document (0-indexed). "
            "Used to derive the ``chunk_id`` for the embedding record."
        ),
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-chunk metadata: heading path, page number, line "
            "range, etc. Propagated onto the embedding record's "
            "``meta`` (top-level) so search-by-meta filters can "
            "target it."
        ),
    )


class IngestResult(BaseModel):
    """Telemetry returned from :meth:`DocumentIngester.ingest`."""

    collection_id: str = Field(..., min_length=1)
    document_id: str = Field(..., min_length=1)
    chunks_indexed: int = Field(
        ...,
        ge=0,
        description="How many chunks were embedded and stored.",
    )
    dimensions: int = Field(
        ...,
        gt=0,
        description=(
            "Vector length used for this ingest. Determined by the "
            "embedder at run time and propagated to "
            ":meth:`VectorStore.create_collection`."
        ),
    )
    replaced: bool = Field(
        ...,
        description=(
            "True if existing chunks for this document were deleted "
            "before re-indexing (``replace=True`` was passed to "
            "``ingest``)."
        ),
    )
    bytes_loaded: int | None = Field(
        default=None,
        description=(
            "Bytes read from the source, if the loader can supply "
            "the count. Useful for telemetry; not load-bearing."
        ),
    )


__all__ = [
    "Chunk",
    "IngestResult",
    "LoadedDocument",
]
