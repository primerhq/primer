"""REST routes for the web fetch providers subsystem.

Two routers mounted under /v1/:

* ``web_fetch_providers_router`` -- generic CRUD on
  :class:`WebFetchProvider`, plus the ``_test`` and ``_types``
  helpers. Built via
  :func:`make_crud_router` with reserved-id guards, cascade-block
  on delete, and on_update / on_delete hooks that invalidate the
  :class:`WebFetchRegistry`.

* ``web_fetch_active_config_router`` -- singleton GET / PUT for
  :class:`ActiveWebFetchConfig`.

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
from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID,
    ActiveWebFetchConfig,
    AggregatedFetchConfig,
    RESERVED_WEB_FETCH_IDS,
    SingleFetchConfig,
    WebFetchProvider,
)


logger = logging.getLogger(__name__)


# ---------- Storage deps -----------------------------------------


def _web_fetch_provider_storage(request: Request):
    return request.app.state.storage_provider.get_storage(WebFetchProvider)


def _active_config_storage(request: Request):
    return request.app.state.storage_provider.get_storage(ActiveWebFetchConfig)


# ---------- Reserved-id guards -----------------------------------


async def _reject_reserved_create(entity, request: Request) -> None:
    """on_pre_create: reject POST at reserved ids."""
    if entity.id in RESERVED_WEB_FETCH_IDS:
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
    if entity_id in RESERVED_WEB_FETCH_IDS:
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
    row = await storage.get(ACTIVE_WEB_FETCH_CONFIG_ID)
    if row is None:
        return
    cfg = row.config
    referenced = False
    if isinstance(cfg, SingleFetchConfig):
        referenced = cfg.provider_id == entity_id
    elif isinstance(cfg, AggregatedFetchConfig):
        referenced = entity_id in cfg.provider_ids
    if referenced:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "cascade_blocked",
                "message": (
                    "currently referenced by the active web fetch "
                    "config; update the active config first"
                ),
                "referenced_by": ACTIVE_WEB_FETCH_CONFIG_ID,
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
    registry = getattr(request.app.state, "web_fetch_registry", None)
    if registry is not None:
        await registry.invalidate(entity_id)


# ---------- Routers ----------------------------------------------


# ---------- _test and _types helpers before CRUD ---------------------------
#
# These routes must be registered on a separate router and mounted
# BEFORE the CRUD router in app.py so that the specific literal paths
# (/web_fetch_providers/_test, /web_fetch_providers/_types) are
# matched before the catch-all /{id} pattern.
#
# Route order matters: FastAPI matches routes in the order they appear
# in the router's routes list. More specific literal routes must come
# before catch-all parameter patterns.


class _ProviderDraft(BaseModel):
    id: str
    provider_type: str
    config: Any


web_fetch_providers_helpers_router = APIRouter(tags=["web-fetch"])


@web_fetch_providers_helpers_router.post(
    "/web_fetch_providers/_test",
    responses=common_responses(500),
    summary=(
        "Test a draft provider config by performing a one-shot fetch. "
        "Builds a transient adapter, runs fetch(url='https://example.com'), "
        "then discards. Returns {ok, title, chars} or {ok=false, error}."
    ),
)
async def test_provider(body: _ProviderDraft) -> dict[str, Any]:
    from primer.api.registries.web_fetch_registry import (
        default_web_fetch_factory,
    )
    from primer.web_fetch.adapter import (
        WebFetchProviderError,
        WebFetchUnavailable,
    )

    # Reconstruct a draft WebFetchProvider (does discriminator validation).
    try:
        draft = WebFetchProvider.model_validate(body.model_dump())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"invalid draft: {exc}"}

    adapter = default_web_fetch_factory(draft)
    try:
        page = await adapter.fetch(url="https://example.com")
    except (WebFetchUnavailable, WebFetchProviderError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - diagnostic-only path
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await adapter.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("test_provider: aclose failed: %s", exc)

    return {"ok": True, "title": page.title, "chars": len(page.content_markdown)}


@web_fetch_providers_helpers_router.get(
    "/web_fetch_providers/_types",
    summary=(
        "Provider-type metadata for the UI form. Returns the field shape "
        "the operator needs to fill in per provider type."
    ),
)
async def list_provider_types() -> dict[str, dict[str, Any]]:
    return {
        "local": {"config_fields": []},
        "jina": {"config_fields": ["api_key"]},
        "firecrawl": {"config_fields": ["api_key"]},
        "exa": {"config_fields": ["api_key"]},
    }


web_fetch_providers_router = make_crud_router(
    model_cls=WebFetchProvider,
    storage_dep=_web_fetch_provider_storage,
    plural="web_fetch_providers",
    tag="web-fetch",
    on_pre_create=_reject_reserved_create,
    on_pre_delete_id=_pre_delete_id_chain,
    on_update=_invalidate_registry,
    on_delete=_invalidate_registry,
)


# ---------- Singleton: ActiveWebFetchConfig ---------------------


web_fetch_active_config_router = APIRouter(tags=["web-fetch"])


async def _validate_referenced_providers(
    config: ActiveWebFetchConfig, request: Request,
) -> None:
    """Validate every provider id referenced by the config exists in
    storage. Raises HTTPException(422) with unknown_ids in the detail
    if any are missing."""
    storage = _web_fetch_provider_storage(request)
    cfg = config.config
    ids: list[str] = []
    if isinstance(cfg, SingleFetchConfig):
        ids = [cfg.provider_id]
    elif isinstance(cfg, AggregatedFetchConfig):
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


@web_fetch_active_config_router.get(
    "/web_fetch_active_config",
    response_model=ActiveWebFetchConfig,
    responses=common_responses(503, 500),
    summary="Read the singleton active web fetch config",
)
async def get_active_config(request: Request) -> ActiveWebFetchConfig:
    storage = _active_config_storage(request)
    row = await storage.get(ACTIVE_WEB_FETCH_CONFIG_ID)
    if row is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "subsystem_not_bootstrapped",
                "message": (
                    "active web fetch config row is missing; bootstrap "
                    "should have created it at startup. Check server "
                    "logs for bootstrap failures."
                ),
            },
        )
    return row


class _ActiveConfigPutBody(BaseModel):
    """Wire body for PUT - wraps the discriminated config so the
    operator doesn't have to include the singleton id in the body."""

    config: Any


@web_fetch_active_config_router.put(
    "/web_fetch_active_config",
    response_model=ActiveWebFetchConfig,
    responses=common_responses(422, 500),
    summary="Replace the singleton active web fetch config",
)
async def put_active_config(
    request: Request,
    body: _ActiveConfigPutBody,
) -> ActiveWebFetchConfig:
    try:
        new_row = ActiveWebFetchConfig(
            id=ACTIVE_WEB_FETCH_CONFIG_ID,
            config=body.config,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_active_config",
                "message": "active web fetch config failed validation",
                "errors": exc.errors(include_url=False),
            },
        )
    await _validate_referenced_providers(new_row, request)
    storage = _active_config_storage(request)
    existing = await storage.get(ACTIVE_WEB_FETCH_CONFIG_ID)
    if existing is None:
        await storage.create(new_row)
    else:
        await storage.update(new_row)
    service = getattr(request.app.state, "web_fetch_service", None)
    if service is not None:
        service.invalidate_active_config()
    return new_row


__all__ = [
    "web_fetch_active_config_router",
    "web_fetch_providers_helpers_router",
    "web_fetch_providers_router",
]
