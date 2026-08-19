"""Internal collections subsystem REST surface.

Endpoints
---------

Config (singleton row at id ``_internal_collections_config``):

* ``PUT    /v1/internal_collections/config`` — upsert the activation
  config. Body shape :class:`InternalCollectionsConfigBody` carries the
  embedding provider + model and optional cross-encoder + MMR knobs.
* ``GET    /v1/internal_collections/config`` — read the row; 404 if
  absent.
* ``DELETE /v1/internal_collections/config`` — clear the row, detach
  the live subsystem, and drop the four reserved collections from the
  backing SSP so a subsequent re-PUT with a different embedding model
  can rebuild cleanly. Custom (non-IC) collections in the same SSP
  are not touched.

Bootstrap:

* ``POST   /v1/internal_collections/bootstrap`` — synchronous
  re-population of every internal collection. Idempotent.

Per-entity search (one per Describeable type):

* ``POST   /v1/agents/search``
* ``POST   /v1/graphs/search``
* ``POST   /v1/collections/search``
* ``POST   /v1/tools/search``

All four return 503 ``type=/errors/subsystem-inactive`` until the
subsystem has been bootstrapped at least once.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_internal_collections_bootstrap_status_storage,
    get_internal_collections_config_storage,
)
from primer.api.errors import (
    PROBLEM_JSON_MEDIA_TYPE,
    ProblemDetails,
    common_responses,
)
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.internal import (
    INTERNAL_COLLECTION_IDS,
    INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID,
    INTERNAL_COLLECTIONS_CONFIG_ID,
    InternalCollectionsBootstrapStatus,
    InternalCollectionsConfig,
)
from primer.model.provider import SemanticSearchProvider
from primer.model.search import CollectionCrossEncoder


logger = logging.getLogger(__name__)


router = APIRouter(tags=["internal-collections"])


# ===========================================================================
# Request bodies
# ===========================================================================


class InternalCollectionsConfigBody(BaseModel):
    """Activation request body for the internal collections subsystem."""

    embedding_provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of the configured EmbeddingProvider to use for every "
            "internal collection. Must reference an existing provider "
            "row at activation time."
        ),
    )
    embedding_model: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side embedding model name. Must be one of the "
            "models permitted on the referenced provider."
        ),
    )
    search_provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of the SemanticSearchProvider that backs the four reserved "
            "internal collections. Must reference an existing SSP row."
        ),
    )
    cross_encoder: CollectionCrossEncoder | None = Field(
        default=None,
        description="Optional cross-encoder reranker config.",
    )


class SearchRequest(BaseModel):
    """Per-entity semantic search body."""

    query: str = Field(
        ...,
        min_length=1,
        description="Free-text query string.",
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of hits to return.",
    )


class SearchHit(BaseModel):
    document_id: str
    chunk_id: str
    score: float | None = None
    text: str
    meta: dict[str, Any] | None = None


class SearchResponse(BaseModel):
    hits: list[SearchHit]


# ===========================================================================
# Helpers
# ===========================================================================


def _subsystem_inactive_response(
    request: Request, detail: str
) -> JSONResponse:
    problem = ProblemDetails(
        type="/errors/subsystem-inactive",
        title="Subsystem Inactive",
        status=503,
        detail=detail,
        instance=request.url.path,
    )
    return JSONResponse(
        status_code=503,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_JSON_MEDIA_TYPE,
    )


def _get_subsystem_or_none(request: Request):
    return getattr(request.app.state, "internal_collections", None)


# ===========================================================================
# Config endpoints
# ===========================================================================


@router.put(
    "/internal_collections/config",
    summary="Activate / re-configure the internal collections subsystem",
    response_model=InternalCollectionsConfig,
    responses=common_responses(404, 422, 500),
)
async def put_config(
    request: Request,
    body: InternalCollectionsConfigBody,
    storage=Depends(get_internal_collections_config_storage),
) -> InternalCollectionsConfig:
    # Validate that search_provider_id references an existing SSP row.
    storage_provider = request.app.state.storage_provider
    ssp_storage = storage_provider.get_storage(SemanticSearchProvider)
    ssp_row = await ssp_storage.get(body.search_provider_id)
    if ssp_row is None:
        raise NotFoundError(
            f"search_provider_id {body.search_provider_id!r} does not refer "
            "to a known SemanticSearchProvider."
        )

    existing = await storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)

    # Vector-space-defining fields are frozen once embeddings exist.
    # Changing the embedding model (or the provider that backs it, or
    # the SSP that holds the vectors) post-activation would mix vectors
    # from incompatible spaces — the new query embeddings can't be
    # compared meaningfully against the old stored ones. The only sane
    # mutation path is DELETE + PUT + bootstrap, which the deactivate
    # button does. cross_encoder is a reranking concern that doesn't
    # touch the vector space, so they stay editable.
    if existing is not None and existing.activated_at is not None:
        frozen_diffs = []
        if body.embedding_provider_id != existing.embedding_provider_id:
            frozen_diffs.append("embedding_provider_id")
        if body.embedding_model != existing.embedding_model:
            frozen_diffs.append("embedding_model")
        if body.search_provider_id != existing.search_provider_id:
            frozen_diffs.append("search_provider_id")
        if frozen_diffs:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "subsystem_active",
                    "message": (
                        f"Cannot change {', '.join(frozen_diffs)} while the "
                        "subsystem is active — these fields define the vector "
                        "space and mixing them would corrupt search results. "
                        "Deactivate the subsystem first (DELETE "
                        "/v1/internal_collections/config), then re-configure "
                        "and re-bootstrap."
                    ),
                    "frozen_fields": frozen_diffs,
                },
            )

    cfg = InternalCollectionsConfig(
        id=INTERNAL_COLLECTIONS_CONFIG_ID,
        embedding_provider_id=body.embedding_provider_id,
        embedding_model=body.embedding_model,
        search_provider_id=body.search_provider_id,
        cross_encoder=body.cross_encoder,
        activated_at=None,
    )
    if existing is None:
        await storage.create(cfg)
    else:
        # Preserve the prior activated_at so an update doesn't appear
        # to deactivate the subsystem.
        cfg = cfg.model_copy(update={"activated_at": existing.activated_at})
        await storage.update(cfg)
    return cfg


@router.get(
    "/internal_collections/config",
    summary="Read the internal collections subsystem config",
    response_model=InternalCollectionsConfig,
    responses=common_responses(404, 500),
)
async def get_config(
    storage=Depends(get_internal_collections_config_storage),
) -> InternalCollectionsConfig:
    row = await storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
    if row is None:
        raise NotFoundError(
            "internal collections subsystem is not configured; PUT "
            "/v1/internal_collections/config to activate."
        )
    return row


@router.delete(
    "/internal_collections/config",
    summary="Clear the internal collections subsystem config",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=common_responses(404, 500),
)
async def delete_config(
    request: Request,
    storage=Depends(get_internal_collections_config_storage),
) -> None:
    row = await storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
    if row is None:
        raise NotFoundError(
            "internal collections subsystem is not configured; nothing "
            "to delete."
        )
    # S2: deactivating also disables semantic search on the system
    # collection and drops its vectors, so a re-activation with a
    # different embedding model cannot surface stale dimensions.
    from primer.knowledge.lifecycle import disable_search
    from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID

    try:
        await disable_search(
            request.app.state.storage_provider,
            request.app.state.semantic_search_registry,
            collection_id=SYSTEM_COLLECTION_ID,
        )
    except NotFoundError:
        pass  # nothing to disable

    # Detach the live subsystem first so the CDC worker stops writing
    # and so the search routes flip to 503 before we touch the vectors.
    subsystem = _get_subsystem_or_none(request)
    if subsystem is not None:
        await subsystem.aclose()
    # Drop the four reserved internal collections from the SSP's
    # backing store. Without this, a subsequent re-PUT with a
    # different embedding model would surface a dimension mismatch on
    # the orphaned vectors. Drops are idempotent — a missing
    # collection is a no-op, and a per-collection failure logs and
    # moves on so a single bad drop doesn't strand the config row.
    semantic_search_registry = getattr(
        request.app.state, "semantic_search_registry", None,
    )
    if semantic_search_registry is not None:
        try:
            store = await semantic_search_registry.get_store(
                row.search_provider_id
            )
        except Exception as exc:  # noqa: BLE001 - tolerate registry errors
            logger.warning(
                "ic deactivate: cannot resolve store for ssp %r: %s; "
                "skipping collection drops (operator must wipe manually)",
                row.search_provider_id, exc,
            )
            store = None
        if store is not None:
            for coll_id in INTERNAL_COLLECTION_IDS.values():
                try:
                    await store.drop_collection(coll_id)
                    logger.info(
                        "ic deactivate: dropped collection %r from ssp %r",
                        coll_id, row.search_provider_id,
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort
                    logger.warning(
                        "ic deactivate: drop_collection(%r) on ssp %r failed: %s",
                        coll_id, row.search_provider_id, exc,
                    )
    # Clear in-memory subsystem state.
    if subsystem is not None:
        request.app.state.internal_collections = None
    # Finally, remove the config row. Doing this last means a partial
    # failure leaves the config in place so the operator can retry the
    # DELETE (drop_collection is idempotent, so retry is safe).
    await storage.delete(INTERNAL_COLLECTIONS_CONFIG_ID)


# ===========================================================================
# Bootstrap endpoints
# ===========================================================================


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _build_subsystem_for_request(
    request: Request, cfg: InternalCollectionsConfig
):
    """Build + attach the live subsystem when the row landed after boot.

    Shared between the bootstrap launcher and the (future) restart-time
    construction path. Returns the freshly attached subsystem.
    """
    from primer.internal_collections import build_subsystem

    provider_registry = request.app.state.provider_registry
    semantic_search_registry = request.app.state.semantic_search_registry
    storage_provider = request.app.state.storage_provider
    # Every built-in (reserved-id) toolset must be listed here or its
    # tools never get embedded and the ``_internal_tools`` semantic
    # search misses them. Mirrors the lifespan path in primer/api/app.py
    # — keep both lists in sync when adding new reserved toolsets.
    toolsets: dict[str, Any] = {}
    sys_ts = getattr(request.app.state, "system_toolset", None)
    if sys_ts is not None:
        toolsets["system"] = sys_ts
    ws_ts = getattr(request.app.state, "workspaces_toolset", None)
    if ws_ts is not None:
        toolsets["workspaces"] = ws_ts
    misc_ts = getattr(request.app.state, "misc_toolset", None)
    if misc_ts is not None:
        toolsets["misc"] = misc_ts
    web_ts = getattr(request.app.state, "web_toolset", None)
    if web_ts is not None:
        toolsets["web"] = web_ts
    harness_ts = getattr(request.app.state, "harness_toolset", None)
    if harness_ts is not None:
        toolsets["harness"] = harness_ts
    trigger_ts = getattr(request.app.state, "trigger_toolset", None)
    if trigger_ts is not None:
        toolsets["trigger"] = trigger_ts
    workspace_ext_ts = getattr(
        request.app.state, "workspace_ext_toolset", None
    )
    if workspace_ext_ts is not None:
        toolsets["workspace_ext"] = workspace_ext_ts
    subsystem = build_subsystem(
        config=cfg,
        storage_provider=storage_provider,
        provider_registry=provider_registry,
        semantic_search_registry=semantic_search_registry,
        toolset_providers=toolsets,
    )
    request.app.state.internal_collections = subsystem
    return subsystem


async def _read_status(
    storage,
) -> InternalCollectionsBootstrapStatus:
    """Return the current status row, or a fresh idle row if missing."""
    row = await storage.get(INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID)
    if row is not None:
        return row
    return InternalCollectionsBootstrapStatus(
        id=INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID,
        status="idle",
    )


async def _upsert_status(
    storage, row: InternalCollectionsBootstrapStatus,
) -> None:
    existing = await storage.get(row.id)
    if existing is None:
        await storage.create(row)
    else:
        await storage.update(row)


async def _run_bootstrap_in_background(
    *,
    app,
    subsystem,
    attempt_id: str,
    status_storage,
) -> None:
    """asyncio.Task body: runs the long bootstrap, streams progress
    into the status row, sets the terminal state at the end.

    Catches *all* exceptions so a failure during a worker-style job
    still results in a structured failure row the UI can render —
    rather than a silent task that the user has no way to see.
    """
    # Throttle writes: the orchestrator emits a tick per page (200
    # entities) which can be every ~50ms for a fast in-memory store.
    # Coalesce so we write at most once per ~250ms.
    _MIN_WRITE_INTERVAL_S = 0.25
    last_write = 0.0
    last_progress = {"phase": None}

    async def _progress(progress) -> None:
        nonlocal last_write
        now_mono = asyncio.get_event_loop().time()
        phase_changed = progress.phase != last_progress["phase"]
        if not phase_changed and (now_mono - last_write) < _MIN_WRITE_INTERVAL_S:
            return
        last_progress["phase"] = progress.phase
        last_write = now_mono
        # Re-read the row before each update so an updated_at column
        # in the underlying storage stays monotonic and a concurrent
        # status write (e.g. boot recovery clearing a stale row) loses
        # the race cleanly via attempt_id mismatch.
        current = await _read_status(status_storage)
        if current.attempt_id != attempt_id:
            # Our row was overwritten by a newer attempt; stop updating.
            raise asyncio.CancelledError("bootstrap status row preempted")
        await _upsert_status(status_storage, current.model_copy(update={
            "phase": progress.phase,
            "phase_done": progress.phase_done,
            "phase_total": progress.phase_total,
            "counts": progress.counts,
        }))

    try:
        result = await subsystem.bootstrap(progress_callback=_progress)
    except asyncio.CancelledError:
        # Bootstrap row was preempted (rare). Don't touch the row.
        logger.info("ic bootstrap (attempt=%s) preempted by newer attempt", attempt_id)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("ic bootstrap failed (attempt=%s)", attempt_id)
        current = await _read_status(status_storage)
        if current.attempt_id == attempt_id:
            await _upsert_status(status_storage, current.model_copy(update={
                "status": "failed",
                "finished_at": _now(),
                "error": f"{type(exc).__name__}: {exc}"[:1024],
            }))
        return

    current = await _read_status(status_storage)
    if current.attempt_id != attempt_id:
        return
    await _upsert_status(status_storage, current.model_copy(update={
        "status": "succeeded",
        "phase": None,
        "finished_at": _now(),
        "error": None,
        "counts": result.get("counts", {}),
    }))
    logger.info(
        "ic bootstrap succeeded (attempt=%s) counts=%s",
        attempt_id, result.get("counts"),
    )


@router.post(
    "/internal_collections/bootstrap",
    summary="Start (or restart) the bootstrap pipeline",
    responses=common_responses(404, 409, 500),
)
async def bootstrap(
    request: Request,
    config_storage=Depends(get_internal_collections_config_storage),
    status_storage=Depends(get_internal_collections_bootstrap_status_storage),
) -> dict:
    """Enable semantic search on the system collection and index it.

    S2: the old asynchronous bootstrap pipeline is gone. Enabling runs
    inline and returns the outcome, so there is nothing to poll and no
    in-flight row to race on. The system collection itself is regenerated
    unconditionally at startup and is readable without any of this; this
    toggle governs vectorisation only.
    """
    cfg = await config_storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
    if cfg is None:
        raise NotFoundError(
            "internal collections subsystem is not configured; PUT "
            "/v1/internal_collections/config first."
        )

    # Stamp activation FIRST: the subsystem snapshots its config at
    # construction, so stamping afterwards leaves its is_activated gate
    # shut and the per-entity search routes answering 503.
    if cfg.activated_at is None:
        cfg = cfg.model_copy(update={"activated_at": _now()})
        await config_storage.update(cfg)

    # The subsystem is still CONSTRUCTED (never started): the reserved
    # `search` toolset resolves through it, and pinned decision 15 keeps
    # that window open until P5 removes the toolset itself.
    subsystem = _get_subsystem_or_none(request)
    if subsystem is None:
        await _build_subsystem_for_request(request, cfg)

    # S2: "bootstrap" is now exactly "enable semantic search on the system
    # collection". The collection itself is regenerated unconditionally at
    # startup and is readable without any of this; the toggle only governs
    # vectorisation, and the config's provider ids map straight onto a
    # CollectionSearchConfig.
    from primer.knowledge.lifecycle import enable_search
    from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID
    from primer.model.collection import CollectionEmbedder, CollectionSearchConfig

    search_cfg = CollectionSearchConfig(
        embedder=CollectionEmbedder(
            provider_id=cfg.embedding_provider_id, model=cfg.embedding_model,
        ),
        vector_store_provider_id=cfg.search_provider_id,
        cross_encoder=cfg.cross_encoder,
    )
    updated = await enable_search(
        request.app.state.storage_provider,
        request.app.state.provider_registry,
        request.app.state.semantic_search_registry,
        collection_id=SYSTEM_COLLECTION_ID,
        cfg=search_cfg,
    )
    state = updated.search.state if updated.search else "disabled"
    return {
        "status": "succeeded" if state == "ready" else "failed",
        "state": state,
        "collection_id": SYSTEM_COLLECTION_ID,
        "error": updated.search.error if updated.search else None,
        "search": updated.search.model_dump(mode="json") if updated.search else None,
    }


@router.get(
    "/internal_collections/bootstrap/status",
    summary="Current bootstrap progress / lifecycle state",
    responses=common_responses(500),
)
async def bootstrap_status(
    request: Request,
    status_storage=Depends(get_internal_collections_bootstrap_status_storage),
) -> dict:
    """Return the singleton status row.

    Always returns 200 — when no bootstrap has ever run, a synthetic
    ``status='idle'`` row is returned so the UI doesn't need to
    distinguish "no row yet" from "row says idle".
    """
    from primer.knowledge.lifecycle import search_status
    from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID

    status = await search_status(
        request.app.state.storage_provider,
        request.app.state.semantic_search_registry,
        collection_id=SYSTEM_COLLECTION_ID,
    )
    body = status.model_dump(mode="json")
    # ``status`` mirrors the lifecycle state in the terminal vocabulary the
    # console already polls on; ``state`` is the authoritative field.
    body["status"] = {
        "ready": "succeeded", "error": "failed",
        "indexing": "running", "disabled": "idle",
    }[status.state]
    return body


# ===========================================================================
# Per-entity search endpoints (Agent / Graph / Collection / Tool)
# ===========================================================================


def _make_search_route(entity_type: str, plural: str) -> None:
    @router.post(
        f"/{plural}/search",
        summary=f"Semantic search over {plural}",
        response_model=SearchResponse,
        responses={
            **common_responses(422, 500),
            503: {
                "model": ProblemDetails,
                "description": "Internal collections subsystem inactive",
                "content": {PROBLEM_JSON_MEDIA_TYPE: {}},
            },
        },
    )
    async def _search(
        body: SearchRequest, request: Request
    ):
        subsystem = _get_subsystem_or_none(request)
        if subsystem is None:
            return _subsystem_inactive_response(
                request,
                "internal collections subsystem is not active; configure "
                "it via PUT /v1/internal_collections/config and run "
                "POST /v1/internal_collections/bootstrap.",
            )
        try:
            hits = await subsystem.search(
                entity_type,  # type: ignore[arg-type]
                query=body.query,
                top_k=body.top_k,
            )
        except ConfigError as exc:
            return _subsystem_inactive_response(request, str(exc))
        return SearchResponse(
            hits=[
                SearchHit(
                    document_id=hit.record.document_id,
                    chunk_id=hit.record.chunk_id,
                    score=hit.score,
                    text=hit.record.text,
                    meta=hit.record.meta,
                )
                for hit in hits
            ]
        )

    _search.__name__ = f"search_{plural}"


_make_search_route("agent", "agents")
_make_search_route("graph", "graphs")
_make_search_route("collection", "collections")
_make_search_route("tool", "tools")


__all__ = [
    "InternalCollectionsConfigBody",
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
    "router",
]
