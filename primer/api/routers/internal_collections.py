"""Internal collections subsystem REST surface.

Endpoints
---------

Config (singleton row at id ``_internal_collections_config``):

* ``PUT    /v1/internal_collections/config`` — upsert the activation
  config. Body shape :class:`InternalCollectionsConfigBody` carries the
  embedding provider + model and optional cross-encoder + MMR knobs.
* ``GET    /v1/internal_collections/config`` — read the row; 404 if
  absent.
* ``DELETE /v1/internal_collections/config`` — clear the row and
  detach the live subsystem (vector tables + collection rows are
  preserved so data is not lost).

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
    INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID,
    INTERNAL_COLLECTIONS_CONFIG_ID,
    InternalCollectionsBootstrapStatus,
    InternalCollectionsConfig,
)
from primer.model.provider import SemanticSearchProvider
from primer.model.search import CollectionCrossEncoder, MmrConfig


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
    mmr: MmrConfig | None = Field(
        default=None,
        description="Optional Maximal Marginal Relevance diversification config.",
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
    # button does. cross_encoder/mmr are reranking concerns that don't
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
        mmr=body.mmr,
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
    await storage.delete(INTERNAL_COLLECTIONS_CONFIG_ID)
    # Detach the live subsystem (vector data preserved). Stop the worker.
    subsystem = _get_subsystem_or_none(request)
    if subsystem is not None:
        await subsystem.aclose()
        request.app.state.internal_collections = None
        request.app.state.provider_registry._search_toolset_provider = None  # noqa: SLF001


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
    from primer.toolset.search import build_search_toolset

    provider_registry = request.app.state.provider_registry
    semantic_search_registry = request.app.state.semantic_search_registry
    storage_provider = request.app.state.storage_provider
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
    subsystem = build_subsystem(
        config=cfg,
        storage_provider=storage_provider,
        provider_registry=provider_registry,
        semantic_search_registry=semantic_search_registry,
        toolset_providers=toolsets,
    )
    request.app.state.internal_collections = subsystem
    search_ts = build_search_toolset(subsystem)
    provider_registry._search_toolset_provider = search_ts  # noqa: SLF001
    request.app.state.search_toolset = search_ts
    subsystem.register_toolset_provider("search", search_ts)
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
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start (or restart) the bootstrap pipeline",
    responses=common_responses(404, 409, 500),
)
async def bootstrap(
    request: Request,
    config_storage=Depends(get_internal_collections_config_storage),
    status_storage=Depends(get_internal_collections_bootstrap_status_storage),
) -> dict:
    """Kick off the bootstrap pipeline as a background task.

    Returns 202 immediately with the freshly-claimed status row.
    Subsequent polls of ``GET /bootstrap/status`` watch progress. A
    second POST while one is already running returns 409 with the
    in-flight row — preventing concurrent attempts that would race on
    the vector tables.
    """
    cfg = await config_storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
    if cfg is None:
        raise NotFoundError(
            "internal collections subsystem is not configured; PUT "
            "/v1/internal_collections/config first."
        )

    current = await _read_status(status_storage)
    if current.status == "running":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "bootstrap_already_running",
                "message": (
                    "A bootstrap is already in progress. Poll "
                    "GET /v1/internal_collections/bootstrap/status."
                ),
                "status": current.model_dump(mode="json"),
            },
        )

    subsystem = _get_subsystem_or_none(request)
    if subsystem is None:
        subsystem = await _build_subsystem_for_request(request, cfg)

    attempt_id = uuid.uuid4().hex
    fresh = InternalCollectionsBootstrapStatus(
        id=INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID,
        status="running",
        phase=None,
        phase_done=0,
        phase_total=None,
        counts={"agents": 0, "graphs": 0, "collections": 0, "tools": 0},
        started_at=_now(),
        finished_at=None,
        error=None,
        attempt_id=attempt_id,
    )
    await _upsert_status(status_storage, fresh)

    # Hold a reference to the task on app.state so it doesn't get GC'd
    # mid-run (asyncio only weak-refs background tasks otherwise).
    task = asyncio.create_task(_run_bootstrap_in_background(
        app=request.app,
        subsystem=subsystem,
        attempt_id=attempt_id,
        status_storage=status_storage,
    ))
    bg_tasks: set[asyncio.Task] = getattr(
        request.app.state, "ic_bootstrap_tasks", None,
    ) or set()
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    request.app.state.ic_bootstrap_tasks = bg_tasks

    return fresh.model_dump(mode="json")


@router.get(
    "/internal_collections/bootstrap/status",
    summary="Current bootstrap progress / lifecycle state",
    responses=common_responses(500),
)
async def bootstrap_status(
    status_storage=Depends(get_internal_collections_bootstrap_status_storage),
) -> dict:
    """Return the singleton status row.

    Always returns 200 — when no bootstrap has ever run, a synthetic
    ``status='idle'`` row is returned so the UI doesn't need to
    distinguish "no row yet" from "row says idle".
    """
    row = await _read_status(status_storage)
    return row.model_dump(mode="json")


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
