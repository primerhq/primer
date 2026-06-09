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

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from primer.model.common import Describeable, Identifiable
from primer.model.search import CollectionSearch


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


class Collection(Describeable):
    """A set of documents searchable by similarity / semantic / hybrid search.

    Inherits ``id`` and ``description`` from :class:`Describeable`. The
    ``embedder`` field selects how the collection's documents are
    vectorised; all documents in a collection share the same embedding
    space.
    """

    _id_prefix: ClassVar[str] = "collection"

    embedder: CollectionEmbedder = Field(
        ...,
        description=(
            "Embedding provider and model used to vectorise this "
            "collection's documents."
        ),
    )
    search_provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of the SemanticSearchProvider backing this collection's "
            "vector index. Bound at create; immutable thereafter (see "
            "Collection PUT validator — wired in Task 5)."
        ),
    )
    search: CollectionSearch | None = Field(
        default=None,
        description=(
            "Optional retrieval-augmentation toggles applied on top "
            "of the base vector search. ``None`` (default) means "
            "vanilla vector ranking; setting ``mmr`` adds Maximal "
            "Marginal Relevance diversification; setting ``cer`` "
            "adds cross-encoder reranking; setting both runs "
            "``vector → cross-encoder rerank → MMR``. See "
            ":class:`primer.model.search.CollectionSearch`."
        ),
    )
    system: bool = Field(
        default=False,
        description=(
            "Marks the collection as system-managed (created and "
            "owned by an internal subsystem like the "
            "SemanticCatalog). Future Collection-CRUD APIs MUST "
            "refuse delete and update on system collections. "
            "Defaults to False; legacy rows without this field "
            "deserialise as user collections."
        ),
    )
    harness_id: str | None = Field(
        default=None,
        description=(
            "When set, this row is managed by the named harness. "
            "Mutation through the public CRUD endpoints returns 409 — "
            "use the harness's sync/uninstall flow instead."
        ),
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

    collection_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the Collection this document belongs to.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Human-readable name of the document.",
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form metadata. Useful for filtering search results, "
            "tagging, audit trails, etc. Schema is application-defined."
        ),
    )
    harness_id: str | None = Field(
        default=None,
        description=(
            "When set, this row is managed by the named harness. "
            "Mutation through the public CRUD endpoints returns 409 — "
            "use the harness's sync/uninstall flow instead."
        ),
    )
