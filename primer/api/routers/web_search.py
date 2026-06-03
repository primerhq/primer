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
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from primer.api.errors import common_responses
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


# ---------- Singleton: ActiveWebSearchConfig ---------------------


web_search_active_config_router = APIRouter(tags=["web-search"])


async def _validate_referenced_providers(
    config: ActiveWebSearchConfig, request: Request,
) -> None:
    """Validate every provider id referenced by the config exists in
    storage. Raises HTTPException(422) with unknown_ids in the detail
    if any are missing."""
    storage = _web_search_provider_storage(request)
    cfg = config.config
    ids: list[str] = []
    if isinstance(cfg, SingleProviderConfig):
        ids = [cfg.provider_id]
    elif isinstance(cfg, AggregatedProviderConfig):
        ids = list(cfg.provider_ids)
    unknown: list[str] = []
    for pid in ids:
        if await storage.get(pid) is None:
            unknown.append(pid)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown_provider_ids",
                "message": (
                    f"active config references unknown provider id(s): "
                    f"{unknown}"
                ),
                "unknown_ids": unknown,
            },
        )


@web_search_active_config_router.get(
    "/web_search_active_config",
    response_model=ActiveWebSearchConfig,
    responses=common_responses(503, 500),
    summary="Read the singleton active web search config",
)
async def get_active_config(request: Request) -> ActiveWebSearchConfig:
    storage = _active_config_storage(request)
    row = await storage.get(ACTIVE_WEB_SEARCH_CONFIG_ID)
    if row is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "subsystem_not_bootstrapped",
                "message": (
                    "active web search config row is missing; bootstrap "
                    "should have created it at startup. Check server "
                    "logs for bootstrap failures."
                ),
            },
        )
    return row


class _ActiveConfigPutBody(BaseModel):
    """Wire body for PUT — wraps the discriminated config so the
    operator doesn't have to include the singleton id in the body."""

    config: Any


@web_search_active_config_router.put(
    "/web_search_active_config",
    response_model=ActiveWebSearchConfig,
    responses=common_responses(422, 500),
    summary="Replace the singleton active web search config",
)
async def put_active_config(
    request: Request,
    body: _ActiveConfigPutBody,
) -> ActiveWebSearchConfig:
    try:
        new_row = ActiveWebSearchConfig(
            id=ACTIVE_WEB_SEARCH_CONFIG_ID,
            config=body.config,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_active_config",
                "message": "active web search config failed validation",
                "errors": exc.errors(include_url=False),
            },
        )
    await _validate_referenced_providers(new_row, request)
    storage = _active_config_storage(request)
    existing = await storage.get(ACTIVE_WEB_SEARCH_CONFIG_ID)
    if existing is None:
        await storage.create(new_row)
    else:
        await storage.update(new_row)
    service = getattr(request.app.state, "web_search_service", None)
    if service is not None:
        service.invalidate_active_config()
    return new_row


__all__ = [
    "web_search_active_config_router",
    "web_search_providers_router",
]
