"""CRUD router for SemanticSearchProvider (/v1/ssp).

Follows the same pattern as :mod:`matrix.api.routers.providers` —
wraps :func:`make_crud_router` with per-entity hooks for invalidation
and cascade-block-on-delete.

Cascade-block (§5 reference-integrity):
    DELETE /v1/ssp/{id} is rejected with 409 when any Collection row
    references ``search_provider_id == id``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request

from matrix.api.deps import get_semantic_search_registry, get_semantic_search_storage
from matrix.api.errors import common_responses
from matrix.api.routers._crud import make_crud_router
from matrix.model.except_ import ConflictError
from matrix.model.provider import SemanticSearchProvider
from matrix.model.storage import FieldRef, Op, OffsetPage, Predicate, Value


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
# CRUD hook: on_delete — cascade-block + invalidate
# ---------------------------------------------------------------------------


async def _on_delete(entity_id: str, request: Request) -> None:
    """Block delete when a Collection references this SSP; then invalidate."""
    from matrix.model.collection import Collection

    storage_provider = request.app.state.storage_provider
    collection_storage = storage_provider.get_storage(Collection)

    predicate = Predicate(
        left=FieldRef(name="search_provider_id"),
        op=Op.EQ,
        right=Value(value=entity_id),
    )
    page = OffsetPage(offset=0, length=1)

    result = await collection_storage.find(predicate, page)
    collisions = result.items if hasattr(result, "items") else []

    if collisions:
        collection_ids = [c.id for c in collisions]
        raise ConflictError(
            f"Cannot delete SemanticSearchProvider {entity_id!r}: "
            f"referenced by Collection(s): {collection_ids}"
        )

    # No collisions — invalidate the cached adapter.
    registry = getattr(request.app.state, "semantic_search_registry", None)
    if registry is not None:
        await registry.invalidate(entity_id)


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
    on_delete=_on_delete,
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
