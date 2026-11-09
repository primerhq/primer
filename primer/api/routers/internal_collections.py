"""Internal collections subsystem REST surface.

Endpoints
---------

Config (singleton row at id ``_internal_collections_config``):

* ``PUT    /v1/internal_collections/config`` — upsert the activation
  config. Body shape :class:`InternalCollectionsConfigBody` carries the
  embedding provider + model and optional cross-encoder + MMR knobs.
* ``GET    /v1/internal_collections/config`` — read the row; 404 if
  absent, unless ``?allow_missing=true``, which answers ``200
  {"configured": false}`` instead (the console asks that way: a 404 is
  a red "Failed to load resource" console error even when it is the
  expected "off" state).
* ``DELETE /v1/internal_collections/config`` - clear the row, disable search
  on the system collection, and drop the four reserved collections from the
  backing SSP so a subsequent re-PUT with a different embedding model
  can rebuild cleanly. Custom (non-IC) collections in the same SSP
  are not touched.

Bootstrap:

* ``POST   /v1/internal_collections/bootstrap`` - enables semantic search
  on the system collection and indexes it, inline. Idempotent.
* ``GET    /v1/internal_collections/bootstrap/status`` - the system
  collection's search state in the console's polling vocabulary.

The row is the system collection's search toggle. The per-entity ``/search``
routes and the subsystem behind them were removed (D18).
"""

from __future__ import annotations

from primer.api.route_tools import route_tool

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_internal_collections_bootstrap_status_storage,
    get_internal_collections_config_storage,
)
from primer.api.errors import (
    common_responses,
)
from primer.model.except_ import NotFoundError
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


# ===========================================================================
# Helpers
# ===========================================================================


# ===========================================================================
# Config endpoints
# ===========================================================================


@router.put(
    "/internal_collections/config",
    summary="Activate / re-configure the internal collections subsystem",
    response_model=InternalCollectionsConfig,
    responses=common_responses(404, 422, 500),
)
@route_tool(scoped_id="internal_collections__put_config")
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


class InternalCollectionsNotConfigured(BaseModel):
    """The ``GET /internal_collections/config?allow_missing=true`` answer when the subsystem has no config row."""

    configured: Literal[False] = Field(
        default=False, description="Always false: no config row exists, so the subsystem is off.",
    )


@router.get(
    "/internal_collections/config",
    summary="Read the internal collections subsystem config",
    response_model=InternalCollectionsConfig | InternalCollectionsNotConfigured,
    responses=common_responses(404, 500),
)
@route_tool(scoped_id="internal_collections__get_config")
async def get_config(
    allow_missing: bool = Query(
        default=False,
        description=(
            "Answer 200 {\"configured\": false} instead of 404 when the subsystem is not configured. For a caller (the console) that "
            "probes this route on every open and treats \"not configured\" as an ordinary state: a 404 is logged by the browser as an "
            "error. Does not change the answer for a configured subsystem."
        ),
    ),
    storage=Depends(get_internal_collections_config_storage),
) -> InternalCollectionsConfig | InternalCollectionsNotConfigured:
    row = await storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
    if row is None and allow_missing:
        return InternalCollectionsNotConfigured()
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
@route_tool(scoped_id="internal_collections__delete_config")
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
    # Finally, remove the config row. Doing this last means a partial
    # failure leaves the config in place so the operator can retry the
    # DELETE (drop_collection is idempotent, so retry is safe).
    await storage.delete(INTERNAL_COLLECTIONS_CONFIG_ID)


# ===========================================================================
# Bootstrap endpoints
# ===========================================================================


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


@router.post(
    "/internal_collections/bootstrap",
    summary="Start (or restart) the bootstrap pipeline",
    responses=common_responses(404, 409, 500),
)
@route_tool(scoped_id="internal_collections__bootstrap")
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

    if cfg.activated_at is None:
        cfg = cfg.model_copy(update={"activated_at": _now()})
        await config_storage.update(cfg)

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
@route_tool(scoped_id="internal_collections__bootstrap_status")
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


__all__ = [
    "InternalCollectionsConfigBody",
    "router",
]
