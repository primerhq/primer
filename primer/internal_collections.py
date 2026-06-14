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
(:data:`primer.model.internal.INTERNAL_COLLECTION_IDS`). Tools are not
persisted as storage rows; the bootstrap enumerates every Toolset row
plus the reserved ``_system`` and ``_search`` toolset providers and
calls ``list_tools()`` on each.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from primer.model.agent import Agent
from primer.model.chat import TextPart
from primer.model.embedding import ExtendedEmbeddingConfig
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.except_ import (
    ConfigError,
    ConflictError,
    DimensionMismatchError,
    NotFoundError,
    PrimerError,
)
from primer.model.graph import Graph
from primer.model.internal import (
    AI_DOCS_COLLECTION_ID,
    BootstrapPhase,
    INTERNAL_COLLECTION_IDS,
    INTERNAL_COLLECTIONS_CONFIG_ID,
    IngestFailure,
    InternalCollectionsConfig,
)
from primer.model.provider import Toolset
from primer.model.storage import OffsetPage
from primer.model.vector import EmbeddingRecord, SearchResult


if TYPE_CHECKING:
    from primer.api.registries import ProviderRegistry
    from primer.api.registries.semantic_search_registry import SemanticSearchRegistry
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


EntityType = Literal["agent", "graph", "collection", "tool"]


# Order matters when building tool document ids: keep the separator
# distinct enough that it can't collide with toolset ids or tool ids.
TOOL_DOC_ID_SEP = "::"


# Phase -> Bootstrap callback contract. Implemented by the router as
# "write the new phase into the singleton status row"; passed in by
# default as a no-op so unit tests can call bootstrap() without
# wiring storage.
BootstrapProgressCallback = Callable[
    ["BootstrapProgress"], Awaitable[None]
]


@dataclass(frozen=True)
class BootstrapProgress:
    """A single tick the orchestrator emits while bootstrapping.

    Either a phase transition (``phase_done == 0``, ``counts`` unchanged
    or extended) or per-item progress within a phase (same ``phase``
    with growing ``phase_done`` / ``counts``).
    """

    phase: BootstrapPhase
    phase_done: int
    phase_total: int | None
    counts: dict[str, int]


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
        semantic_search_registry: "SemanticSearchRegistry",
        toolset_providers: dict[str, ToolsetProviderLike] | None = None,
        queue_max: int = 10_000,
    ) -> None:
        """Initialize the subsystem with dependencies.

        Args:
            config: The InternalCollectionsConfig row defining embedding and
                search provider details.
            storage_provider: StorageProvider for accessing entity and config rows.
            provider_registry: ProviderRegistry for embedder access.
            semantic_search_registry: SemanticSearchRegistry for per-call semantic
                search store access using config.search_provider_id.
            toolset_providers: Optional pre-seeded mapping of reserved toolset ids
                to their providers (e.g., _system, _search).
            queue_max: Max size of the CDC event queue before dropping events.
        """
        self._config = config
        self._sp = storage_provider
        self._pr = provider_registry
        self._semantic_search_registry = semantic_search_registry
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
        store = await self._semantic_search_registry.get_store(
            self._config.search_provider_id
        )
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

    async def _embed_text(
        self, text: str, *, task_type: str = "retrieval_document",
    ) -> list[float]:
        """Embed ``text`` for either ingest (``retrieval_document``,
        default) or search (``retrieval_query``).

        Asymmetric-retrieval models (BGE, E5, nomic-embed-text) embed
        queries and documents in slightly different sub-spaces — pass
        ``retrieval_query`` at search time so the HuggingFace adapter
        applies the model-family-specific instruction prefix. Without
        this hint, a "web search" query against the indexed
        ``web-search: Perform a web search…`` tool description scores
        ~0.25 cosine on BGE; with it, it scores ~0.7+.
        """
        embedder = await self._pr.get_embedder(self._config.embedding_provider_id)
        response = await embedder.embed(
            model=self._config.embedding_model,
            inputs=[TextPart(text=text)],
            config=ExtendedEmbeddingConfig(task_type=task_type),
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

    async def bootstrap(
        self,
        *,
        progress_callback: BootstrapProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Materialise collections + ingest every existing entity + tool.

        When ``progress_callback`` is supplied, it's awaited at every
        phase transition and after every N (currently 10) ingested
        entities. Used by the async router to surface progress in the
        ``bootstrap_status`` row without coupling this module to
        storage.
        """
        counts: dict[str, int] = {
            "agents": 0, "graphs": 0, "collections": 0, "tools": 0,
            "docs": 0,
        }

        async def _emit(phase: BootstrapPhase, done: int, total: int | None) -> None:
            if progress_callback is None:
                return
            await progress_callback(BootstrapProgress(
                phase=phase, phase_done=done, phase_total=total,
                counts=dict(counts),
            ))

        logger.info("ic bootstrap: phase=drain_queue")
        await _emit("drain_queue", 0, None)
        await self._drain_queue()

        logger.info("ic bootstrap: phase=materialise_collections")
        await _emit("materialise_collections", 0, len(INTERNAL_COLLECTION_IDS))
        await self._materialise_collection_rows()
        await _emit("materialise_collections", len(INTERNAL_COLLECTION_IDS), len(INTERNAL_COLLECTION_IDS))

        store = await self._semantic_search_registry.get_store(
            self._config.search_provider_id
        )
        embed_dim = await self._probe_embedding_dim()
        for entity_type in INTERNAL_COLLECTION_IDS:
            # Idempotent: re-bootstrap reuses the collection if it
            # already exists. If the store doesn't support exists_ok,
            # we catch + swallow the "already exists" path.
            await self._ensure_collection(store, INTERNAL_COLLECTION_IDS[entity_type], embed_dim)
        # 5th reserved collection — agent-facing platform docs.
        await self._ensure_collection(store, AI_DOCS_COLLECTION_ID, embed_dim)

        logger.info("ic bootstrap: phase=ingest_agents")
        counts["agents"] = await self._ingest_persisted_with_progress(
            "agent", Agent, "ingest_agents", _emit, counts,
        )
        logger.info("ic bootstrap: agents=%d", counts["agents"])

        logger.info("ic bootstrap: phase=ingest_graphs")
        counts["graphs"] = await self._ingest_persisted_with_progress(
            "graph", Graph, "ingest_graphs", _emit, counts,
        )
        logger.info("ic bootstrap: graphs=%d", counts["graphs"])

        logger.info("ic bootstrap: phase=ingest_collections")
        counts["collections"] = await self._ingest_persisted_with_progress(
            "collection", Collection, "ingest_collections", _emit, counts,
        )
        logger.info("ic bootstrap: collections=%d", counts["collections"])

        logger.info("ic bootstrap: phase=ingest_tools")
        counts["tools"] = await self._ingest_tools_with_progress(
            "ingest_tools", _emit, counts,
        )
        logger.info("ic bootstrap: tools=%d", counts["tools"])

        logger.info("ic bootstrap: phase=ingest_ai_docs")
        counts["docs"] = await self._ingest_ai_docs(_emit, counts)
        logger.info("ic bootstrap: docs=%d", counts["docs"])

        logger.info("ic bootstrap: phase=finalize")
        await _emit("finalize", 0, None)
        self._config = self._config.model_copy(
            update={"activated_at": _now()}
        )
        await self._upsert_config_row(self._config)
        self.start_worker()

        logger.info("ic bootstrap: complete counts=%s", counts)
        return {
            "ok": True,
            "counts": counts,
            "activated_at": self._config.activated_at,
        }

    async def _ensure_collection(
        self, store, collection_id: str, dimensions: int,
    ) -> None:
        """Create the vector-store collection if it doesn't already
        exist, otherwise leave it intact.

        Some backends (lance) raise on create when the collection
        already exists -- for re-bootstrap we need this call to be
        idempotent so the operator can re-run any time without first
        dropping anything. The "exists" check is store-specific; we
        try the create then suppress the typical existence-error
        signatures rather than introspecting each store's API.

        Raises :class:`~primer.model.except_.DimensionMismatchError` (422)
        when the collection already exists in the store but was created
        with a different embedding dimension. This surfaces a meaningful
        error at bootstrap time instead of producing silent indexing
        failures or 400s at query time.
        """
        try:
            await store.create_collection(collection_id, dimensions=dimensions)
            return
        except ConflictError as exc:
            # The store already holds this collection with a different
            # dimension -- a new embedder model is being activated against
            # vectors produced by the old one. Raise a typed 422 with a
            # re-index hint so the operator sees what to fix.
            import re as _re
            m = _re.search(r"dimensions=(\d+)", str(exc))
            stored_dim = int(m.group(1)) if m else 0
            raise DimensionMismatchError(
                f"Internal collection {collection_id!r} is stored with "
                f"dimension {stored_dim} but the active embedder "
                f"({self._config.embedding_model!r} via provider "
                f"{self._config.embedding_provider_id!r}) produces "
                f"dimension {dimensions}. Deactivate the internal "
                f"collections subsystem (DELETE "
                f"/v1/internal_collections/config), then re-configure "
                f"and re-bootstrap with the correct embedding model.",
                embedder_dim=dimensions,
                collection_dim=stored_dim,
                collection_id=collection_id,
                cause=exc,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if (
                "already exist" in msg
                or "duplicate" in msg
                or "exists" in msg
            ):
                logger.debug(
                    "ic bootstrap: collection %s already exists, reusing",
                    collection_id,
                )
                return
            raise

    async def _ingest_persisted_with_progress(
        self,
        entity_type: EntityType,
        model_cls: type,
        phase: BootstrapPhase,
        emit: Callable[[BootstrapPhase, int, int | None], Awaitable[None]],
        counts: dict[str, int],
    ) -> int:
        """Variant of :meth:`_ingest_persisted` that updates the running
        counts dict in place and emits per-page progress ticks. The
        per-entity tick rate is intentionally page-grained, not item-
        grained — keeps the status-row writes proportional to actual
        work, not network chatter."""
        counts_key = entity_type + "s"
        storage = self._sp.get_storage(model_cls)
        # Cheap total estimate via list(length=0) — most backends return
        # the total alongside the empty page. SQLite/Postgres do; the
        # in-memory fake does too. If a backend can't, we fall through
        # to phase_total=None and the UI shows an indeterminate bar.
        try:
            head = await storage.list(OffsetPage(offset=0, length=0))
            total: int | None = getattr(head, "total", None)
        except Exception:  # pragma: no cover — defensive
            total = None

        await emit(phase, 0, total)
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
            counts[counts_key] = count
            await emit(phase, count, total)
            if len(page.items) < page_size:
                break
            offset += page_size
        return count

    async def _ingest_tools_with_progress(
        self,
        phase: BootstrapPhase,
        emit: Callable[[BootstrapPhase, int, int | None], Awaitable[None]],
        counts: dict[str, int],
    ) -> int:
        """Variant of :meth:`_ingest_tools` that ticks counts as
        toolsets are processed. Tool totals aren't known up-front (tool
        lists are pulled lazily from each provider), so we emit
        ``phase_total=None`` and let the UI render an indeterminate
        progress bar."""
        count = 0
        seen_toolset_ids: set[str] = set()
        await emit(phase, 0, None)

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
                counts["tools"] = count
                await emit(phase, count, None)
            if len(page.items) < page_size:
                break
            offset += page_size

        for ts_id, provider in self._toolset_providers.items():
            if ts_id in seen_toolset_ids:
                continue
            count += await self._ingest_one_toolset(ts_id, provider)
            counts["tools"] = count
            await emit(phase, count, None)

        return count

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
                search_provider_id=self._config.search_provider_id,
            )
            existing = await collections.get(coll_id)
            if existing is None:
                await collections.create(row)
            else:
                await collections.update(row)
        # Fifth reserved collection — agent-facing platform docs.
        # Multi-chunk records produced by DocumentIngester at ingest
        # time; row only carries identity + provider linkage.
        ai_docs_row = Collection(
            id=AI_DOCS_COLLECTION_ID,
            description=(
                "Reserved internal collection holding agent-facing "
                "platform documentation. Sourced from the markdown "
                "files shipped in primer.ai_docs."
            ),
            embedder=embedder,
            system=True,
            search_provider_id=self._config.search_provider_id,
        )
        existing = await collections.get(AI_DOCS_COLLECTION_ID)
        if existing is None:
            await collections.create(ai_docs_row)
        else:
            await collections.update(ai_docs_row)

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
        except (NotFoundError, PrimerError):  # pragma: no cover
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
        except PrimerError as exc:
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

    # ---- AI docs ingest (5th reserved collection) --------------------

    # Default location of the agent-facing markdown source files. Lives
    # inside the primer package so the docs ship with the wheel; the
    # path is resolved relative to ``primer/__init__.py`` so it works
    # under both editable installs and packaged ones.
    @staticmethod
    def _default_ai_docs_path() -> "Path":
        from primer.ai_docs_path import resolve_ai_docs_dir

        return resolve_ai_docs_dir()

    async def _ingest_ai_docs(
        self,
        emit: Callable[[BootstrapPhase, int, int | None], Awaitable[None]],
        counts: dict[str, int],
        *,
        ai_docs_path: "Path | None" = None,
        ingester_factory: Callable[..., Any] | None = None,
    ) -> int:
        """Walk markdown files, embed via DocumentIngester, content-hash skip.

        Each ``docs/agents/<slug>.md`` becomes one
        :class:`~primer.model.collection.Document` (id=``<slug>``) under
        the reserved ``_internal_ai_docs`` collection. Multi-chunk
        records are produced by :class:`~primer.ingest.DocumentIngester`
        — its Docling-backed default splitter cuts on Markdown headings,
        so retrieval returns the specific subsection (e.g.
        ``yields → Gotchas``) rather than the whole doc.

        Skips re-embedding files whose ``content_hash`` in
        ``Document.meta`` matches the file's current sha256.
        Re-embedded files replace prior chunks via ``replace=True``.

        Failures per file log a WARN + IngestFailure row; one bad doc
        doesn't fail the bootstrap.
        """
        import hashlib
        import re
        from pathlib import Path

        from primer.ingest.ingester import DocumentIngester
        from primer.model.collection import Collection as CollectionModel
        from primer.model.collection import Document

        root = ai_docs_path or self._default_ai_docs_path()
        await emit("ingest_ai_docs", 0, None)
        if not root.exists() or not root.is_dir():
            logger.info(
                "ic bootstrap: ai_docs directory not present at %s, "
                "skipping ingest_ai_docs phase",
                root,
            )
            counts["docs"] = 0
            await emit("ingest_ai_docs", 0, 0)
            return 0

        files = sorted(p for p in root.rglob("*.md") if p.is_file())
        # Skip files starting with "_" to allow internal notes (e.g.
        # _design.md, _README.md) that shouldn't be ingested.
        files = [f for f in files if not f.name.startswith("_")]
        total = len(files)
        await emit("ingest_ai_docs", 0, total)

        if total == 0:
            counts["docs"] = 0
            return 0

        documents = self._sp.get_storage(Document)
        collections = self._sp.get_storage(CollectionModel)
        ai_docs_collection = await collections.get(AI_DOCS_COLLECTION_ID)
        if ai_docs_collection is None:
            # Should not happen — _materialise_collection_rows runs first
            logger.warning(
                "ic bootstrap: _internal_ai_docs collection row missing; "
                "ingest_ai_docs phase skipped"
            )
            return 0

        store = await self._semantic_search_registry.get_store(
            self._config.search_provider_id
        )
        embedder = await self._pr.get_embedder(
            self._config.embedding_provider_id
        )
        if ingester_factory is None:
            ingester = DocumentIngester(
                collection=ai_docs_collection,
                embedder=embedder,
                vector_store=store,
            )
        else:
            ingester = ingester_factory(
                collection=ai_docs_collection,
                embedder=embedder,
                vector_store=store,
            )

        # Lightweight frontmatter extractor — YAML between leading
        # ``---`` lines. Keeps doc metadata (title/summary/related)
        # available on the Document.meta payload for richer search
        # results without pulling pyyaml into the import path.
        _fm_re = re.compile(
            r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL,
        )

        def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
            m = _fm_re.match(text)
            if not m:
                return {}, text
            block = m.group(1)
            body = text[m.end():]
            meta: dict[str, Any] = {}
            current_key: str | None = None
            list_items: list[str] | None = None
            for raw in block.splitlines():
                line = raw.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                if line.startswith("- ") and current_key is not None:
                    if list_items is None:
                        list_items = []
                        meta[current_key] = list_items
                    list_items.append(line[2:].strip())
                    continue
                if ":" in line and not line.startswith(" "):
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    current_key = key
                    list_items = None
                    if val == "":
                        meta[key] = None
                    elif val.startswith("[") and val.endswith("]"):
                        inner = val[1:-1].strip()
                        meta[key] = (
                            [piece.strip() for piece in inner.split(",") if piece.strip()]
                            if inner else []
                        )
                    else:
                        # Strip simple quotes.
                        if (
                            (val.startswith("\"") and val.endswith("\""))
                            or (val.startswith("'") and val.endswith("'"))
                        ):
                            val = val[1:-1]
                        meta[key] = val
            return meta, body

        ingested = 0
        skipped = 0
        for idx, path in enumerate(files, start=1):
            slug = path.relative_to(root).with_suffix("").as_posix()
            try:
                raw_bytes = path.read_bytes()
                content_hash = hashlib.sha256(raw_bytes).hexdigest()
                raw_text = raw_bytes.decode("utf-8", errors="replace")
                frontmatter, body = _parse_frontmatter(raw_text)
                title = frontmatter.get("title") or slug.replace("-", " ").title()
                summary = frontmatter.get("summary") or ""

                # Skip path: existing Document with matching content hash.
                existing = await documents.get(slug)
                if (
                    existing is not None
                    and existing.meta.get("content_hash") == content_hash
                ):
                    skipped += 1
                    counts["docs"] = ingested + skipped
                    await emit("ingest_ai_docs", idx, total)
                    continue

                doc_meta: dict[str, Any] = {
                    "slug": slug,
                    "title": title,
                    "summary": summary,
                    "content_hash": content_hash,
                    "source_path": str(path),
                }
                related = frontmatter.get("related")
                if related:
                    doc_meta["related"] = related
                mcp_tools = frontmatter.get("mcp_tools")
                if mcp_tools:
                    doc_meta["mcp_tools"] = mcp_tools

                doc = Document(
                    id=slug,
                    collection_id=AI_DOCS_COLLECTION_ID,
                    name=title,
                    meta=doc_meta,
                )
                if existing is None:
                    await documents.create(doc)
                else:
                    await documents.update(doc)

                # Run the chunking + embedding pipeline. ``replace=True``
                # drops any previously-indexed chunks for this doc so a
                # content edit doesn't leave stale chunks behind.
                await ingester.ingest(doc, Path(path), replace=True)
                ingested += 1
            except Exception as exc:  # noqa: BLE001 — one bad doc shouldn't fail bootstrap
                logger.warning(
                    "ic bootstrap: ai_docs ingest failed for %s: %s: %s",
                    path,
                    type(exc).__name__,
                    exc,
                )
                event = IngestEvent(
                    op="upsert",
                    entity_type="tool",  # closest enum value; meta carries real type
                    entity_id=f"_internal_ai_docs::{slug}",
                    payload={"source_path": str(path), "kind": "ai_doc"},
                )
                await self._log_failure(event, exc)
            counts["docs"] = ingested + skipped
            await emit("ingest_ai_docs", idx, total)

        logger.info(
            "ic bootstrap: ai_docs ingested=%d skipped=%d (total files=%d)",
            ingested,
            skipped,
            total,
        )
        return ingested + skipped

    async def search_ai_docs(
        self,
        *,
        query: str,
        top_k: int = 10,
    ) -> list[SearchResult]:
        """Semantic search over agent-facing platform docs.

        Separate entry point from :meth:`search` because the docs
        collection isn't keyed off a per-entity-type CDC pipeline —
        records here come from disk-based ingest only.
        """
        if not self.is_activated:
            raise ConfigError(
                "internal collections subsystem is configured but has "
                "not been bootstrapped yet; POST "
                "/v1/internal_collections/bootstrap to populate the "
                "collections."
            )
        store = await self._semantic_search_registry.get_store(
            self._config.search_provider_id
        )
        vector = await self._embed_text(query, task_type="retrieval_query")
        return await store.search(AI_DOCS_COLLECTION_ID, vector, top_k)

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
        store = await self._semantic_search_registry.get_store(
            self._config.search_provider_id
        )
        vector = await self._embed_text(query, task_type="retrieval_query")
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
    semantic_search_registry: "SemanticSearchRegistry",
    toolset_providers: dict[str, ToolsetProviderLike] | None = None,
) -> InternalCollectionsSubsystem:
    """Construct a subsystem instance. Caller starts the worker."""
    return InternalCollectionsSubsystem(
        config=config,
        storage_provider=storage_provider,
        provider_registry=provider_registry,
        semantic_search_registry=semantic_search_registry,
        toolset_providers=toolset_providers,
    )


__all__ = [
    "AI_DOCS_COLLECTION_ID",
    "INTERNAL_COLLECTION_IDS",
    "InternalCollectionsSubsystem",
    "IngestEvent",
    "ToolsetProviderLike",
    "build_subsystem",
    "embedding_text_for",
    "load_config_or_none",
]
