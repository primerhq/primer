"""REST routes for the web search providers subsystem.

Two routers mounted under /v1/:

* ``web_search_providers_router`` -- generic CRUD on
  :class:`WebSearchProvider`, plus the ``_test`` and ``_types``
  helpers (the helpers land in Task 6.3). Built via
  :func:`make_crud_router` with reserved-id guards, cascade-block
  on delete, and on_update / on_delete hooks that invalidate the
  :class:`WebSearchRegistry`.

* ``web_search_active_config_router`` -- singleton GET / PUT for
  :class:`ActiveWebSearchConfig` (lands in Task 6.2).

Cascade-block target: the active-config singleton's ``provider_id``
(single mode) or ``provider_ids`` (aggregated mode). Custom pre-delete
hook walks the singleton row because the reference lives inside a
discriminated union -- the standard ReferenceCheck mechanism doesn't
fit that shape.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from primer.api.routers._crud import make_crud_router
from primer.model.web_search import (
    ACTIVE_WEB_SEARCH_CONFIG_ID,
    ActiveWebSearchConfig,
    AggregatedProviderConfig,
    RESERVED_WEB_SEARCH_IDS,
    SingleProviderConfig,
    WebSearchProvider,
)


logger = logging.getLogger(__name__)


# ---------- Storage deps -----------------------------------------


def _web_search_provider_storage(request: Request):
    return request.app.state.storage_provider.get_storage(WebSearchProvider)


def _active_config_storage(request: Request):
    return request.app.state.storage_provider.get_storage(ActiveWebSearchConfig)


# ---------- Reserved-id guards -----------------------------------


async def _reject_reserved_create(entity, request: Request) -> None:
    """on_pre_create: reject POST at reserved ids."""
    if entity.id in RESERVED_WEB_SEARCH_IDS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "reserved_id",
                "message": (
                    f"id {entity.id!r} is reserved for the bootstrap-managed "
                    "provider; choose a different id"
                ),
            },
        )


async def _reject_reserved_delete(entity_id: str, request: Request) -> None:
    """on_pre_delete_id: reject DELETE on reserved ids (fires before
    storage lookup so it works even when the row exists)."""
    if entity_id in RESERVED_WEB_SEARCH_IDS:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_id",
                "message": (
                    f"id {entity_id!r} is reserved; the bootstrap-managed "
                    "provider cannot be deleted"
                ),
            },
        )


# ---------- Cascade-block on delete ------------------------------


async def _cascade_block_on_active_config(
    entity_id: str, request: Request,
) -> None:
    """on_pre_delete_id: 409 if the active config references the row.

    The reference lives inside a discriminated union, so we read the
    singleton row directly rather than using ReferenceCheck.
    """
    storage = _active_config_storage(request)
    row = await storage.get(ACTIVE_WEB_SEARCH_CONFIG_ID)
    if row is None:
        return
    cfg = row.config
    referenced = False
    if isinstance(cfg, SingleProviderConfig):
        referenced = cfg.provider_id == entity_id
    elif isinstance(cfg, AggregatedProviderConfig):
        referenced = entity_id in cfg.provider_ids
    if referenced:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "cascade_blocked",
                "message": (
                    "currently referenced by the active web search "
                    "config; update the active config first"
                ),
                "referenced_by": ACTIVE_WEB_SEARCH_CONFIG_ID,
            },
        )


async def _pre_delete_id_chain(entity_id: str, request: Request) -> None:
    """Compose: reserved-id guard, then cascade-block."""
    await _reject_reserved_delete(entity_id, request)
    await _cascade_block_on_active_config(entity_id, request)


# ---------- Registry invalidation hooks --------------------------


async def _invalidate_registry(entity_id: str, request: Request) -> None:
    """on_update / on_delete: invalidate the cached adapter so the
    next call to ``registry.get(entity_id)`` reconstructs from storage."""
    registry = getattr(request.app.state, "web_search_registry", None)
    if registry is not None:
        await registry.invalidate(entity_id)


# ---------- Routers ----------------------------------------------


web_search_providers_router = make_crud_router(
    model_cls=WebSearchProvider,
    storage_dep=_web_search_provider_storage,
    plural="web_search_providers",
    tag="web-search",
    on_pre_create=_reject_reserved_create,
    on_pre_delete_id=_pre_delete_id_chain,
    on_update=_invalidate_registry,
    on_delete=_invalidate_registry,
)


__all__ = ["web_search_providers_router"]
