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

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from primer.api.deps import get_internal_collections_config_storage
from primer.api.errors import (
    PROBLEM_JSON_MEDIA_TYPE,
    ProblemDetails,
    common_responses,
)
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.internal import (
    INTERNAL_COLLECTIONS_CONFIG_ID,
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

    cfg = InternalCollectionsConfig(
        id=INTERNAL_COLLECTIONS_CONFIG_ID,
        embedding_provider_id=body.embedding_provider_id,
        embedding_model=body.embedding_model,
        search_provider_id=body.search_provider_id,
        cross_encoder=body.cross_encoder,
        mmr=body.mmr,
        activated_at=None,
    )
    existing = await storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
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
# Bootstrap endpoint
# ===========================================================================


@router.post(
    "/internal_collections/bootstrap",
    summary="Populate (or repopulate) every internal collection",
    responses=common_responses(404, 500, 502, 504),
)
async def bootstrap(
    request: Request,
    storage=Depends(get_internal_collections_config_storage),
) -> dict:
    """Materialise collections + vector tables, ingest every entity + tool.

    Idempotent — re-runnable any time. Builds the live subsystem if
    one does not yet exist (i.e. config was added after boot), then
    invokes the orchestrator. Activates the ``_search`` toolset and
    starts the CDC worker on completion.
    """
    cfg = await storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
    if cfg is None:
        raise NotFoundError(
            "internal collections subsystem is not configured; PUT "
            "/v1/internal_collections/config first."
        )

    subsystem = _get_subsystem_or_none(request)
    if subsystem is None:
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

    return await subsystem.bootstrap()


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
