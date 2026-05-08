"""Internal semantic catalog over Describeable entities.

The :class:`SemanticCatalog` is an event-source-agnostic subsystem
that indexes :class:`Agent`, :class:`Tool`, :class:`Graph`, and
:class:`Collection` entities into four per-type system Collections so
any entity becomes discoverable by natural-language query. It exposes
``index / delete / search`` and nothing else; whatever drives those
calls (a future event bus, a migration script, a one-shot
backfill) lives outside this module.

Design highlights (see
``docs/superpowers/specs/2026-05-08-semantic-catalog-design.md``):

* The four backing collections are real :class:`Collection` rows with
  ``system=True`` in the application's collection storage. They reuse
  the existing ``Embedder`` / ``VectorStore`` / ``CollectionSearcher``
  plumbing, so MMR / cross-encoder reranking can later be enabled by
  toggling ``Collection.search`` on the system rows.
* Tool entity ids are scoped — ``toolset_id__bare_name`` — to avoid
  collisions when two toolsets expose tools with the same bare name.
  The catalog trusts the convention but defends against unscoped
  inputs by re-scoping when ``__`` is absent from ``tool.id``.
* Embedded text is ``f"{entity.id}\\n\\n{entity.description}"``: the
  id appears first so machine-readable names with semantic content
  (``code-reviewer``, ``web-search``) remain discoverable even when
  the description is generic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from matrix.model.agent import Agent
from matrix.model.chat import TextPart, Tool
from matrix.model.collection import Collection, CollectionEmbedder
from matrix.model.common import Describeable
from matrix.model.except_ import BadRequestError, ConfigError
from matrix.model.graph import Graph
from matrix.model.vector import EmbeddingRecord
from matrix.search.searcher import CollectionSearcher

from matrix.catalog.types import SemanticEntityType, SemanticHit


if TYPE_CHECKING:
    from matrix.int.embedder import Embedder
    from matrix.int.storage import Storage
    from matrix.int.vector_store import VectorStore


logger = logging.getLogger(__name__)


# Tool ids surfaced anywhere outside a ToolsetProvider's internal
# registry are scoped as ``toolset_id<sep>bare_name``. Mirrors the
# constant in ``matrix.agent.tool_manager``; duplicated here so the
# catalog package has no agent-package dependency.
_TOOL_SCOPE_SEPARATOR = "__"


_DEFAULT_DESCRIPTIONS: dict[SemanticEntityType, str] = {
    SemanticEntityType.AGENT: (
        "System collection for semantic search over Agent definitions. "
        "Managed by SemanticCatalog; do not edit."
    ),
    SemanticEntityType.TOOL: (
        "System collection for semantic search over Tool descriptors. "
        "Managed by SemanticCatalog; do not edit."
    ),
    SemanticEntityType.GRAPH: (
        "System collection for semantic search over Graph definitions. "
        "Managed by SemanticCatalog; do not edit."
    ),
    SemanticEntityType.COLLECTION: (
        "System collection for semantic search over user-defined "
        "Collections. Managed by SemanticCatalog; do not edit."
    ),
}


class SemanticCatalog:
    """Internal, event-source-agnostic semantic index over Describeable entities."""

    _COLLECTION_IDS: ClassVar[dict[SemanticEntityType, str]] = {
        SemanticEntityType.AGENT: "_catalog_agents",
        SemanticEntityType.TOOL: "_catalog_tools",
        SemanticEntityType.GRAPH: "_catalog_graphs",
        SemanticEntityType.COLLECTION: "_catalog_collections",
    }

    _TYPE_TO_CLASS: ClassVar[dict[SemanticEntityType, type[Describeable]]] = {
        SemanticEntityType.AGENT: Agent,
        SemanticEntityType.TOOL: Tool,
        SemanticEntityType.GRAPH: Graph,
        SemanticEntityType.COLLECTION: Collection,
    }

    def __init__(
        self,
        *,
        embedder: "Embedder",
        embedder_provider_id: str,
        embedder_model: str,
        vector_store: "VectorStore",
        collection_storage: "Storage[Collection]",
    ) -> None:
        if not embedder_provider_id:
            raise ConfigError("embedder_provider_id must be non-empty")
        if not embedder_model:
            raise ConfigError("embedder_model must be non-empty")
        self._embedder = embedder
        self._embedder_provider_id = embedder_provider_id
        self._embedder_model = embedder_model
        self._vector_store = vector_store
        self._collection_storage = collection_storage
        self._initialized = False
        # Cached searchers (one per entity type). Populated on first
        # search() call so we don't construct one unnecessarily during
        # index / delete-only workloads.
        self._searchers: dict[SemanticEntityType, CollectionSearcher] = {}
        # Cached Collection rows keyed by entity type, populated during
        # initialize().
        self._collections: dict[SemanticEntityType, Collection] = {}

    # ---- Lifecycle -------------------------------------------------------

    async def initialize(self) -> None:
        """Idempotently provision the four system Collection rows.

        Safe to call repeatedly. If a reserved id is already taken by a
        non-system row, or by a system row pointing at a different
        embedder, raises :class:`ConfigError` rather than silently
        clobbering.
        """
        # Probe vector dimensionality once. The probe is also a cheap
        # smoke-test for the embedder, surfacing misconfiguration here
        # rather than on first index().
        probe = await self._embedder.embed(
            model=self._embedder_model,
            inputs=[TextPart(text="catalog probe")],
        )
        if not probe.embeddings:
            raise ConfigError(
                "embedder returned no vector for the catalog probe; "
                "cannot determine dimensionality"
            )
        dimensions = len(probe.embeddings[0].vector)
        if dimensions <= 0:
            raise ConfigError(
                f"embedder returned a zero-length vector "
                f"(dimensions={dimensions})"
            )

        for entity_type, collection_id in self._COLLECTION_IDS.items():
            collection = await self._ensure_collection(
                entity_type=entity_type,
                collection_id=collection_id,
            )
            self._collections[entity_type] = collection
            # VectorStore.create_collection is idempotent on identical
            # args per its contract.
            await self._vector_store.create_collection(
                collection_id,
                dimensions=dimensions,
                distance="cosine",
            )

        self._initialized = True

    async def _ensure_collection(
        self,
        *,
        entity_type: SemanticEntityType,
        collection_id: str,
    ) -> Collection:
        existing = await self._collection_storage.get(collection_id)
        if existing is None:
            new_row = Collection(
                id=collection_id,
                description=_DEFAULT_DESCRIPTIONS[entity_type],
                embedder=CollectionEmbedder(
                    provider_id=self._embedder_provider_id,
                    model=self._embedder_model,
                ),
                system=True,
            )
            return await self._collection_storage.create(new_row)
        if not existing.system:
            raise ConfigError(
                f"reserved id {collection_id!r} is occupied by a "
                "non-system Collection row; refusing to bind it to the "
                "SemanticCatalog"
            )
        if (
            existing.embedder.provider_id != self._embedder_provider_id
            or existing.embedder.model != self._embedder_model
        ):
            raise ConfigError(
                f"system collection {collection_id!r} bound to "
                f"provider/model "
                f"{existing.embedder.provider_id!r}/"
                f"{existing.embedder.model!r}; this catalog is "
                f"configured for "
                f"{self._embedder_provider_id!r}/"
                f"{self._embedder_model!r}. Re-embedding is the "
                "activation API's responsibility, not initialize()'s"
            )
        return existing

    # ---- Public API ------------------------------------------------------

    async def index(
        self,
        entity_type: SemanticEntityType,
        entity: Describeable,
    ) -> None:
        """Embed ``entity`` and upsert it into the per-type collection."""
        self._require_initialized()
        self._validate_entity(entity_type, entity)
        document_id = self._compose_document_id(entity_type, entity)
        text = f"{entity.id}\n\n{entity.description}"

        response = await self._embedder.embed(
            model=self._embedder_model,
            inputs=[TextPart(text=text)],
        )
        if not response.embeddings:
            raise ConfigError(
                f"embedder returned no embedding for {entity_type.value} "
                f"{entity.id!r}"
            )
        vector = list(response.embeddings[0].vector)

        record = EmbeddingRecord(
            collection_id=self._COLLECTION_IDS[entity_type],
            document_id=document_id,
            chunk_id="0",
            text=text,
            vector=vector,
            meta={"entity_type": entity_type.value},
        )
        await self._vector_store.put(record)
        logger.debug(
            "SemanticCatalog indexed entity",
            extra={
                "entity_type": entity_type.value,
                "document_id": document_id,
            },
        )

    async def delete(
        self,
        entity_type: SemanticEntityType,
        entity_id: str,
    ) -> None:
        """Remove the entity's embedding from the per-type collection."""
        self._require_initialized()
        if not entity_id:
            raise BadRequestError("entity_id must be non-empty")
        await self._vector_store.delete(
            self._COLLECTION_IDS[entity_type],
            entity_id,
        )
        logger.debug(
            "SemanticCatalog deleted entity",
            extra={
                "entity_type": entity_type.value,
                "document_id": entity_id,
            },
        )

    async def search(
        self,
        entity_type: SemanticEntityType,
        query: str,
        k: int = 10,
    ) -> list[SemanticHit]:
        """Vector-search the per-type collection; return top-``k`` hits."""
        self._require_initialized()
        if k <= 0:
            raise BadRequestError(f"k must be > 0, got {k!r}")
        if not query:
            raise BadRequestError("query must be non-empty")

        searcher = self._searcher_for(entity_type)
        results = await searcher.search(query, k=k)
        return [
            SemanticHit(
                entity_type=entity_type,
                entity_id=r.record.document_id,
                text=r.record.text,
                score=r.score if r.score is not None else 0.0,
            )
            for r in results
        ]

    # ---- Internals -------------------------------------------------------

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ConfigError(
                "SemanticCatalog: call initialize() before index/delete/search"
            )

    @staticmethod
    def _validate_entity(
        entity_type: SemanticEntityType,
        entity: Describeable,
    ) -> None:
        expected = SemanticCatalog._TYPE_TO_CLASS[entity_type]
        if not isinstance(entity, expected):
            raise BadRequestError(
                f"entity_type={entity_type.value!r} expects "
                f"{expected.__name__} instance; got {type(entity).__name__}"
            )

    @staticmethod
    def _compose_document_id(
        entity_type: SemanticEntityType,
        entity: Describeable,
    ) -> str:
        if entity_type is SemanticEntityType.TOOL:
            assert isinstance(entity, Tool)  # invariant from _validate_entity
            if _TOOL_SCOPE_SEPARATOR in entity.id:
                return entity.id
            # Defence-in-depth: index a tool that hasn't passed through
            # ToolExecutionManager (e.g., direct catalog use during a
            # backfill) with the scoped form composed from toolset_id.
            return f"{entity.toolset_id}{_TOOL_SCOPE_SEPARATOR}{entity.id}"
        return entity.id

    def _searcher_for(
        self,
        entity_type: SemanticEntityType,
    ) -> CollectionSearcher:
        cached = self._searchers.get(entity_type)
        if cached is not None:
            return cached
        collection = self._collections[entity_type]
        searcher = CollectionSearcher(
            collection=collection,
            embedder=self._embedder,
            vector_store=self._vector_store,
        )
        self._searchers[entity_type] = searcher
        return searcher


__all__ = ["SemanticCatalog"]
