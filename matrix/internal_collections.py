"""Internal collections subsystem — bootstrap + CDC + search dispatch.

Concepts
--------

* :class:`InternalCollectionsSubsystem` — the runtime handle that the
  app lifespan attaches to ``app.state.internal_collections`` once the
  config row is present. Owns the CDC worker task, exposes
  ``bootstrap()`` to (re-)populate vectors, ``enqueue()`` for hooks
  to fire change events, ``search()`` for the per-entity search APIs,
  and ``aclose()`` for shutdown.
* :class:`IngestEvent` — runtime DTO for change-data-capture events
  the worker dequeues and applies to the vector store.
* :func:`build_subsystem` — factory used by the lifespan and by
  ``create_test_app`` to construct the subsystem from a config row +
  injected dependencies.

The subsystem is **eventually consistent**: every CDC event the worker
fails to apply is persisted as an :class:`IngestFailure` row for a
future global retry scheduler to replay. The worker itself does NOT
retry — that's the scheduler's job in the next sub-project.

The subsystem ingests four entity types — Agent, Graph, Collection,
Tool — each into its own reserved collection
(:data:`matrix.model.internal.INTERNAL_COLLECTION_IDS`). Tools are not
persisted as storage rows; the bootstrap enumerates every Toolset row
plus the reserved ``_system`` and ``_search`` toolset providers and
calls ``list_tools()`` on each.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from matrix.model.agent import Agent
from matrix.model.chat import TextPart
from matrix.model.collection import Collection, CollectionEmbedder
from matrix.model.except_ import ConfigError, MatrixError, NotFoundError
from matrix.model.graph import Graph
from matrix.model.internal import (
    INTERNAL_COLLECTION_IDS,
    INTERNAL_COLLECTIONS_CONFIG_ID,
    IngestFailure,
    InternalCollectionsConfig,
)
from matrix.model.provider import Toolset
from matrix.model.storage import OffsetPage
from matrix.model.vector import EmbeddingRecord, SearchResult


if TYPE_CHECKING:
    from matrix.api.registries import ProviderRegistry, VectorStoreRegistry
    from matrix.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


EntityType = Literal["agent", "graph", "collection", "tool"]


# Order matters when building tool document ids: keep the separator
# distinct enough that it can't collide with toolset ids or tool ids.
TOOL_DOC_ID_SEP = "::"


# ===========================================================================
# CDC event DTO
# ===========================================================================


@dataclass(slots=True)
class IngestEvent:
    """One change-capture event enqueued by an entity hook.

    The CDC worker dequeues these one at a time and applies them to
    the vector store. ``payload`` is the entity body for ``upsert``
    (used to extract the embedding text); it's ignored for ``delete``.
    """

    op: Literal["upsert", "delete"]
    entity_type: EntityType
    entity_id: str
    payload: dict[str, Any] | None = None


# Toolset providers we ingest tools from must satisfy this protocol —
# the live :class:`InternalToolsetProvider` does, MCP / web do too.
class ToolsetProviderLike:  # pragma: no cover -- structural only
    def list_tools(self) -> AsyncIterator: ...


# ===========================================================================
# Embedding-text extraction (one strategy per entity type)
# ===========================================================================


def _agent_embedding_text(payload: dict[str, Any]) -> str:
    parts = [payload.get("description") or "", *payload.get("system_prompt", [])]
    return "\n\n".join(p for p in parts if p).strip() or payload.get("id", "")


def _graph_embedding_text(payload: dict[str, Any]) -> str:
    parts = [payload.get("description") or ""]
    for node in payload.get("nodes", []):
        nid = node.get("id", "")
        if nid:
            parts.append(f"node {nid}")
    return "\n".join(p for p in parts if p).strip() or payload.get("id", "")


def _collection_embedding_text(payload: dict[str, Any]) -> str:
    return (payload.get("description") or payload.get("id", "")).strip()


def _tool_embedding_text(payload: dict[str, Any]) -> str:
    desc = payload.get("description") or ""
    name = payload.get("id", "")
    return f"{name}: {desc}".strip(": ").strip() or name


_EMBED_TEXT_FOR: dict[EntityType, Any] = {
    "agent": _agent_embedding_text,
    "graph": _graph_embedding_text,
    "collection": _collection_embedding_text,
    "tool": _tool_embedding_text,
}


def embedding_text_for(entity_type: EntityType, payload: dict[str, Any]) -> str:
    """Public helper so the search APIs embed the same way as ingest."""
    return _EMBED_TEXT_FOR[entity_type](payload)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# Subsystem
# ===========================================================================


class InternalCollectionsSubsystem:
    """Live runtime handle for the internal collections subsystem."""

    def __init__(
        self,
        *,
        config: InternalCollectionsConfig,
        storage_provider: "StorageProvider",
        provider_registry: "ProviderRegistry",
        vector_store_registry: "VectorStoreRegistry",
        toolset_providers: dict[str, ToolsetProviderLike] | None = None,
        queue_max: int = 10_000,
    ) -> None:
        self._config = config
        self._sp = storage_provider
        self._pr = provider_registry
        self._vsr = vector_store_registry
        # Reserved-id toolset providers we need to enumerate during
        # bootstrap. The caller injects the live ``_system`` and
        # ``_search`` providers — we deliberately do not reach into
        # the registry's private state.
        self._toolset_providers: dict[str, ToolsetProviderLike] = (
            toolset_providers or {}
        )
        self._queue: asyncio.Queue[IngestEvent] = asyncio.Queue(
            maxsize=queue_max
        )
        self._worker_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @property
    def config(self) -> InternalCollectionsConfig:
        return self._config

    @property
    def is_activated(self) -> bool:
        """``True`` iff the config has been bootstrapped at least once."""
        return self._config.activated_at is not None

    # ---- Worker lifecycle --------------------------------------------

    def start_worker(self) -> None:
        """Idempotent — starts the CDC consumer task if not running."""
        if self._worker_task is None or self._worker_task.done():
            self._stopped.clear()
            self._worker_task = asyncio.create_task(
                self._run_worker(), name="internal-collections-cdc"
            )
            logger.info("internal collections CDC worker started")

    async def aclose(self) -> None:
        """Stop the worker and drain the queue."""
        self._stopped.set()
        if self._worker_task is not None:
            try:
                self._queue.put_nowait(
                    IngestEvent(
                        op="delete",
                        entity_type="agent",
                        entity_id="_shutdown_sentinel",
                    )
                )
            except asyncio.QueueFull:  # pragma: no cover
                pass
            try:
                await asyncio.wait_for(self._worker_task, timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover
                self._worker_task.cancel()
        self._worker_task = None

    # ---- Enqueue (called by mutation hooks) --------------------------

    def enqueue(self, event: IngestEvent) -> None:
        """Best-effort enqueue of a CDC event."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "internal collections CDC queue full; dropping event "
                "%s/%s/%s — re-bootstrap will reconcile",
                event.op,
                event.entity_type,
                event.entity_id,
            )

    def queue_size(self) -> int:
        return self._queue.qsize()

    # ---- Toolset provider injection ----------------------------------

    def register_toolset_provider(
        self, toolset_id: str, provider: ToolsetProviderLike
    ) -> None:
        self._toolset_providers[toolset_id] = provider

    # ---- Worker loop --------------------------------------------------

    async def _run_worker(self) -> None:
        while not self._stopped.is_set():
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:  # pragma: no cover
                return
            if event.entity_id == "_shutdown_sentinel":
                self._queue.task_done()
                return
            try:
                await self._apply_event(event)
            except Exception as exc:  # noqa: BLE001 — failures recorded
                await self._log_failure(event, exc)
            finally:
                self._queue.task_done()

    async def _apply_event(self, event: IngestEvent) -> None:
        store = await self._vsr.get()
        coll_id = INTERNAL_COLLECTION_IDS[event.entity_type]
        if event.op == "delete":
            await store.delete(coll_id, event.entity_id)
            return
        if event.payload is None:
            raise ValueError(
                f"upsert event for {event.entity_type}/{event.entity_id} "
                "missing payload"
            )
        text = embedding_text_for(event.entity_type, event.payload)
        if not text:
            return
        vector = await self._embed_text(text)
        await store.put(
            EmbeddingRecord(
                collection_id=coll_id,
                document_id=event.entity_id,
                chunk_id="0",
                text=text,
                vector=vector,
                meta={"entity_type": event.entity_type, **event.payload},
            )
        )

    async def _embed_text(self, text: str) -> list[float]:
        embedder = await self._pr.get_embedder(self._config.embedding_provider_id)
        response = await embedder.embed(
            model=self._config.embedding_model,
            inputs=[TextPart(text=text)],
        )
        return list(response.embeddings[0].vector)

    async def _log_failure(
        self, event: IngestEvent, exc: BaseException
    ) -> None:
        try:
            failure_storage = self._sp.get_storage(IngestFailure)
            await failure_storage.create(
                IngestFailure(
                    id=str(uuid.uuid4()),
                    entity_type=event.entity_type,
                    entity_id=event.entity_id,
                    op=event.op,
                    error=str(exc),
                    failed_at=_now(),
                    retry_count=0,
                )
            )
        except Exception:  # noqa: BLE001 — last-resort
            logger.exception(
                "double fault: failed to write IngestFailure row for "
                "%s/%s/%s",
                event.op,
                event.entity_type,
                event.entity_id,
            )

    # ---- Bootstrap ----------------------------------------------------

    async def bootstrap(self) -> dict[str, Any]:
        """Materialise collections + ingest every existing entity + tool."""
        await self._drain_queue()
        await self._materialise_collection_rows()

        store = await self._vsr.get()
        embed_dim = await self._probe_embedding_dim()
        for entity_type in INTERNAL_COLLECTION_IDS:
            await store.create_collection(
                INTERNAL_COLLECTION_IDS[entity_type],
                dimensions=embed_dim,
            )

        agent_count = await self._ingest_persisted("agent", Agent)
        graph_count = await self._ingest_persisted("graph", Graph)
        collection_count = await self._ingest_persisted("collection", Collection)
        tool_count = await self._ingest_tools()

        self._config = self._config.model_copy(
            update={"activated_at": _now()}
        )
        await self._upsert_config_row(self._config)
        self.start_worker()

        return {
            "ok": True,
            "counts": {
                "agents": agent_count,
                "graphs": graph_count,
                "collections": collection_count,
                "tools": tool_count,
            },
            "activated_at": self._config.activated_at,
        }

    async def _drain_queue(self) -> None:
        while not self._queue.empty():
            event = self._queue.get_nowait()
            try:
                await self._apply_event(event)
            except Exception as exc:  # noqa: BLE001
                await self._log_failure(event, exc)
            finally:
                self._queue.task_done()

    async def _materialise_collection_rows(self) -> None:
        collections = self._sp.get_storage(Collection)
        embedder = CollectionEmbedder(
            provider_id=self._config.embedding_provider_id,
            model=self._config.embedding_model,
        )
        for entity_type, coll_id in INTERNAL_COLLECTION_IDS.items():
            row = Collection(
                id=coll_id,
                description=(
                    f"Reserved internal collection for {entity_type} "
                    "semantic search."
                ),
                embedder=embedder,
                system=True,
                search_provider_id="_unused_placeholder",  # TODO(task-6): wire real SSP id from InternalCollectionsConfig.search_provider_id
            )
            existing = await collections.get(coll_id)
            if existing is None:
                await collections.create(row)
            else:
                await collections.update(row)

    async def _probe_embedding_dim(self) -> int:
        vec = await self._embed_text("dimensionality probe")
        return len(vec)

    async def _ingest_persisted(
        self, entity_type: EntityType, model_cls: type
    ) -> int:
        storage = self._sp.get_storage(model_cls)
        page_size = 200
        offset = 0
        count = 0
        while True:
            page = await storage.list(
                OffsetPage(offset=offset, length=page_size)
            )
            for entity in page.items:
                payload = entity.model_dump(mode="json")
                event = IngestEvent(
                    op="upsert",
                    entity_type=entity_type,
                    entity_id=entity.id,
                    payload=payload,
                )
                try:
                    await self._apply_event(event)
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    await self._log_failure(event, exc)
            if len(page.items) < page_size:
                break
            offset += page_size
        return count

    async def _ingest_tools(self) -> int:
        count = 0
        seen_toolset_ids: set[str] = set()

        ts_storage = self._sp.get_storage(Toolset)
        offset = 0
        page_size = 200
        while True:
            page = await ts_storage.list(
                OffsetPage(offset=offset, length=page_size)
            )
            for ts in page.items:
                seen_toolset_ids.add(ts.id)
                provider = await self._safe_get_toolset(ts.id)
                count += await self._ingest_one_toolset(ts.id, provider)
            if len(page.items) < page_size:
                break
            offset += page_size

        for ts_id, provider in self._toolset_providers.items():
            if ts_id in seen_toolset_ids:
                continue
            count += await self._ingest_one_toolset(ts_id, provider)

        return count

    async def _safe_get_toolset(self, toolset_id: str):
        try:
            return await self._pr.get_toolset(toolset_id)
        except (NotFoundError, MatrixError):  # pragma: no cover
            return None

    async def _ingest_one_toolset(
        self, toolset_id: str, provider: ToolsetProviderLike | None
    ) -> int:
        if provider is None:
            return 0
        n = 0
        try:
            async for tool in provider.list_tools():
                doc_id = f"{toolset_id}{TOOL_DOC_ID_SEP}{tool.id}"
                payload = tool.model_dump(mode="json")
                payload["toolset_id"] = toolset_id
                event = IngestEvent(
                    op="upsert",
                    entity_type="tool",
                    entity_id=doc_id,
                    payload=payload,
                )
                try:
                    await self._apply_event(event)
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    await self._log_failure(event, exc)
        except MatrixError as exc:
            await self._log_failure(
                IngestEvent(
                    op="upsert",
                    entity_type="tool",
                    entity_id=f"{toolset_id}{TOOL_DOC_ID_SEP}*",
                    payload={"toolset_id": toolset_id},
                ),
                exc,
            )
        return n

    async def _upsert_config_row(
        self, cfg: InternalCollectionsConfig
    ) -> None:
        storage = self._sp.get_storage(InternalCollectionsConfig)
        existing = await storage.get(cfg.id)
        if existing is None:
            await storage.create(cfg)
        else:
            await storage.update(cfg)

    # ---- Search ------------------------------------------------------

    async def search(
        self,
        entity_type: EntityType,
        *,
        query: str,
        top_k: int = 10,
    ) -> list[SearchResult]:
        if not self.is_activated:
            raise ConfigError(
                "internal collections subsystem is configured but has "
                "not been bootstrapped yet; POST "
                "/v1/internal_collections/bootstrap to populate the "
                "collections."
            )
        store = await self._vsr.get()
        vector = await self._embed_text(query)
        coll_id = INTERNAL_COLLECTION_IDS[entity_type]
        return await store.search(coll_id, vector, top_k)


# ===========================================================================
# Factory used by the lifespan handler + create_test_app
# ===========================================================================


async def load_config_or_none(
    storage_provider: "StorageProvider",
) -> InternalCollectionsConfig | None:
    """Read the singleton config row; returns ``None`` if absent."""
    storage = storage_provider.get_storage(InternalCollectionsConfig)
    return await storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)


def build_subsystem(
    *,
    config: InternalCollectionsConfig,
    storage_provider: "StorageProvider",
    provider_registry: "ProviderRegistry",
    vector_store_registry: "VectorStoreRegistry",
    toolset_providers: dict[str, ToolsetProviderLike] | None = None,
) -> InternalCollectionsSubsystem:
    """Construct a subsystem instance. Caller starts the worker."""
    return InternalCollectionsSubsystem(
        config=config,
        storage_provider=storage_provider,
        provider_registry=provider_registry,
        vector_store_registry=vector_store_registry,
        toolset_providers=toolset_providers,
    )


__all__ = [
    "INTERNAL_COLLECTION_IDS",
    "InternalCollectionsSubsystem",
    "IngestEvent",
    "ToolsetProviderLike",
    "build_subsystem",
    "embedding_text_for",
    "load_config_or_none",
]
