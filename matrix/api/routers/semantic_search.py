"""CRUD router for SemanticSearchProvider (/v1/ssp).

Follows the same pattern as :mod:`matrix.api.routers.providers` —
wraps :func:`make_crud_router` with per-entity hooks for invalidation
and cascade-block-on-delete.

Cascade-block (§5 reference-integrity):
    DELETE /v1/ssp/{id} is rejected with 409 when any Collection row
    references ``search_provider_id == id``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from matrix.api.deps import get_semantic_search_registry, get_semantic_search_storage
from matrix.api.errors import common_responses
from matrix.api.registries.provider_registry import RESERVED_SSP_IDS
from matrix.api.routers._crud import make_crud_router
from matrix.api.routers._references import ReferenceCheck
from matrix.model.provider import SemanticSearchProvider


# ---------------------------------------------------------------------------
# Reserved-id protection hooks
# ---------------------------------------------------------------------------


async def _reject_reserved_ssp_create(entity, request: Request) -> None:
    """Reject POST /v1/ssp with a reserved SSP id (409)."""
    if entity.id in RESERVED_SSP_IDS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "reserved_id",
                "kind": "ssp",
                "reserved": sorted(RESERVED_SSP_IDS),
                "message": (
                    f"id {entity.id!r} is reserved and cannot be "
                    "created via the API"
                ),
            },
        )


async def _reject_reserved_ssp_delete(entity_id: str, request: Request) -> None:
    """Reject DELETE /v1/ssp/<reserved-id> (403)."""
    if entity_id in RESERVED_SSP_IDS:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_id_protected",
                "kind": "ssp",
                "message": (
                    f"id {entity_id!r} is a reserved SSP and cannot be deleted"
                ),
            },
        )


# ---------------------------------------------------------------------------
# CRUD hook: on_create (no-op — no adapter to warm)
# ---------------------------------------------------------------------------


async def _on_create(entity_id: str, request: Request) -> None:
    """No-op: SemanticSearchRegistry lazy-constructs on first use."""


# ---------------------------------------------------------------------------
# CRUD hook: on_update — invalidate cached adapter
# ---------------------------------------------------------------------------


async def _on_update(entity_id: str, request: Request) -> None:
    """Invalidate the cached VectorStoreProvider instance for this SSP row.

    Called after PUT /v1/ssp/{id}; the next call to
    SemanticSearchRegistry.get_provider(id) will re-resolve the row
    from storage and reconstruct the live backend.
    """
    registry = getattr(request.app.state, "semantic_search_registry", None)
    if registry is not None:
        await registry.invalidate(entity_id)


# ---------------------------------------------------------------------------
# Storage helper for Collection reference check
# ---------------------------------------------------------------------------


def _get_collection_storage(request: Request):
    from matrix.model.collection import Collection
    return request.app.state.storage_provider.get_storage(Collection)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


semantic_search_router = make_crud_router(
    model_cls=SemanticSearchProvider,
    storage_dep=get_semantic_search_storage,
    plural="ssp",
    tag="semantic-search-providers",
    on_create=_on_create,
    on_update=_on_update,
    on_delete=_on_update,
    on_pre_create=_reject_reserved_ssp_create,
    on_pre_delete_id=_reject_reserved_ssp_delete,
    references=[
        ReferenceCheck(
            child_kind="collection",
            child_storage=_get_collection_storage,
            child_field="search_provider_id",
        ),
    ],
)


@semantic_search_router.post(
    "/ssp/{entity_id}/invalidate",
    status_code=204,
    summary="Invalidate cached SemanticSearch adapter",
    responses=common_responses(500),
)
async def invalidate_semantic_search_provider(
    entity_id: str = Path(..., description="SemanticSearchProvider id"),
    registry=Depends(get_semantic_search_registry),
) -> None:
    await registry.invalidate(entity_id)


__all__ = ["semantic_search_router"]
