"""REST router for triggers + subscriptions.

Endpoints (Spec §10):

* ``POST   /v1/triggers``                           create
* ``GET    /v1/triggers``                           list (?kind, ?enabled)
* ``GET    /v1/triggers/{id}``                      get one
* ``PUT    /v1/triggers/{id}``                      partial update
* ``DELETE /v1/triggers/{id}``                      cascade-delete subs
* ``POST   /v1/triggers/{id}/fire_now``             synchronous fire
* ``POST   /v1/triggers/{id}/subscriptions``        create subscription
* ``GET    /v1/triggers/{id}/subscriptions``        list subscriptions
* ``GET    /v1/triggers/{id}/subscriptions/{sid}``  get one
* ``PUT    /v1/triggers/{id}/subscriptions/{sid}``  partial update
* ``DELETE /v1/triggers/{id}/subscriptions/{sid}``  delete

Error envelope: ``HTTPException(detail={"code": "<error_code>", ...})``
so callers can dispatch on ``response.json()["detail"]["code"]``. The
mapping covers the cases the service layer reports:

* :class:`~primer.trigger.service.TriggerNotFound` /
  :class:`~primer.trigger.service.SubscriptionNotFound` → 404
* :class:`~primer.trigger.service.TriggerKindImmutable` → 409
  ``trigger_kind_immutable``
* :class:`~primer.trigger.service.ParkedSessionOnlyFromYield` → 422
  ``parked_session_only_from_yield``
* :class:`~primer.trigger.service.TriggerSlugConflict` → 409
  ``trigger_slug_conflict``
* :class:`~primer.trigger.cron.CronInvalid` → 422 ``cron_invalid``
* :class:`~primer.trigger.cron.TimezoneInvalid` → 422 ``timezone_invalid``
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_claim_engine,
    get_event_bus,
    get_storage_provider,
)
from primer.model.trigger import (
    Subscription,
    SubscriptionConfig,
    Trigger,
    TriggerConfig,
)
from primer.trigger.cron import CronInvalid, TimezoneInvalid
from primer.trigger.service import (
    ParkedSessionOnlyFromYield,
    ServiceDeps,
    SubscriptionNotFound,
    TriggerKindImmutable,
    TriggerNotFound,
    TriggerSlugConflict,
    create_subscription,
    create_trigger,
    delete_subscription,
    delete_trigger,
    fire_now,
    get_subscription,
    get_trigger,
    list_subscriptions,
    list_triggers,
    update_subscription,
    update_trigger,
)


triggers_router = APIRouter(prefix="/v1/triggers", tags=["triggers"])


# ---------------------------------------------------------------------------
# Request body models
# ---------------------------------------------------------------------------


class TriggerCreateBody(BaseModel):
    slug: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    config: TriggerConfig
    enabled: bool = True


class TriggerUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    config: TriggerConfig | None = None


class SubscriptionCreateBody(BaseModel):
    config: SubscriptionConfig
    payload_template: str | None = None
    parallelism: str = "skip"
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool = True


class SubscriptionUpdateBody(BaseModel):
    # Use the default sentinel pattern: None means "set to null".
    # Omit the key entirely to leave it untouched (FastAPI sets the
    # default below). For nullable optionals we distinguish "absent"
    # from "explicit null" by checking ``model_fields_set``.
    payload_template: str | None = None
    parallelism: str | None = None
    enabled: bool | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deps(
    sp: Any,
    claim_engine: Any | None,
    event_bus: Any | None,
) -> ServiceDeps:
    return ServiceDeps(
        storage_provider=sp,
        claim_engine=claim_engine,
        event_bus=event_bus,
    )


def _raise_code(status: int, code: str, detail: str) -> None:
    """Raise an HTTPException with ``{detail: {code, message}}`` body."""
    raise HTTPException(
        status_code=status,
        detail={"code": code, "message": detail},
    )


# ---------------------------------------------------------------------------
# Trigger CRUD
# ---------------------------------------------------------------------------


@triggers_router.post("", status_code=201, summary="Create a trigger")
async def create_trigger_endpoint(
    body: TriggerCreateBody,
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    try:
        trigger = await create_trigger(
            slug=body.slug,
            name=body.name,
            description=body.description,
            config=body.config,
            enabled=body.enabled,
            deps=deps,
        )
    except TriggerSlugConflict as exc:
        _raise_code(409, "trigger_slug_conflict", str(exc))
    except CronInvalid as exc:
        _raise_code(422, "cron_invalid", str(exc))
    except TimezoneInvalid as exc:
        _raise_code(422, "timezone_invalid", str(exc))
    return JSONResponse(
        status_code=201,
        content=trigger.model_dump(mode="json"),
    )


@triggers_router.get("", summary="List triggers")
async def list_triggers_endpoint(
    kind: Annotated[
        str | None,
        Query(description="Filter by trigger kind (delayed / scheduled)."),
    ] = None,
    enabled: Annotated[
        bool | None, Query(description="Filter by enabled flag."),
    ] = None,
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> dict[str, Any]:
    deps = _deps(sp, claim_engine, event_bus)
    items = await list_triggers(kind=kind, enabled=enabled, deps=deps)
    return {
        "items": [t.model_dump(mode="json") for t in items],
        "total": len(items),
    }


@triggers_router.get("/{trigger_id}", summary="Get a trigger")
async def get_trigger_endpoint(
    trigger_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    try:
        trigger = await get_trigger(trigger_id=trigger_id, deps=deps)
    except TriggerNotFound as exc:
        _raise_code(404, "trigger_not_found", str(exc))
    return JSONResponse(
        status_code=200,
        content=trigger.model_dump(mode="json"),
    )


@triggers_router.put("/{trigger_id}", summary="Update a trigger")
async def update_trigger_endpoint(
    body: TriggerUpdateBody,
    trigger_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    try:
        trigger = await update_trigger(
            trigger_id=trigger_id,
            name=body.name,
            description=body.description,
            enabled=body.enabled,
            config=body.config,
            deps=deps,
        )
    except TriggerNotFound as exc:
        _raise_code(404, "trigger_not_found", str(exc))
    except TriggerKindImmutable as exc:
        _raise_code(409, "trigger_kind_immutable", str(exc))
    except CronInvalid as exc:
        _raise_code(422, "cron_invalid", str(exc))
    except TimezoneInvalid as exc:
        _raise_code(422, "timezone_invalid", str(exc))
    return JSONResponse(
        status_code=200,
        content=trigger.model_dump(mode="json"),
    )


@triggers_router.delete("/{trigger_id}", summary="Delete a trigger (cascade)")
async def delete_trigger_endpoint(
    trigger_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    try:
        await delete_trigger(trigger_id=trigger_id, deps=deps)
    except TriggerNotFound as exc:
        _raise_code(404, "trigger_not_found", str(exc))
    return JSONResponse(status_code=204, content=None)


@triggers_router.post(
    "/{trigger_id}/fire_now",
    summary="Synchronously fire a trigger",
)
async def fire_now_endpoint(
    trigger_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    try:
        result = await fire_now(trigger_id=trigger_id, deps=deps)
    except TriggerNotFound as exc:
        _raise_code(404, "trigger_not_found", str(exc))
    return JSONResponse(
        status_code=200,
        content={
            "skipped": result.skipped,
            "fire_id": result.fire_id,
            "results": result.results,
        },
    )


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------


@triggers_router.post(
    "/{trigger_id}/subscriptions",
    status_code=201,
    summary="Create a subscription",
)
async def create_subscription_endpoint(
    body: SubscriptionCreateBody,
    trigger_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    try:
        sub = await create_subscription(
            trigger_id=trigger_id,
            config=body.config,
            payload_template=body.payload_template,
            parallelism=body.parallelism,
            description=body.description,
            enabled=body.enabled,
            deps=deps,
        )
    except TriggerNotFound as exc:
        _raise_code(404, "trigger_not_found", str(exc))
    except ParkedSessionOnlyFromYield as exc:
        _raise_code(422, "parked_session_only_from_yield", str(exc))
    return JSONResponse(
        status_code=201,
        content=sub.model_dump(mode="json"),
    )


@triggers_router.get(
    "/{trigger_id}/subscriptions",
    summary="List subscriptions for a trigger",
)
async def list_subscriptions_endpoint(
    trigger_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> dict[str, Any]:
    deps = _deps(sp, claim_engine, event_bus)
    # Confirm the parent trigger exists so we surface 404 instead of
    # an empty list when the caller used a stale id.
    try:
        await get_trigger(trigger_id=trigger_id, deps=deps)
    except TriggerNotFound as exc:
        _raise_code(404, "trigger_not_found", str(exc))
    items = await list_subscriptions(trigger_id=trigger_id, deps=deps)
    return {
        "items": [s.model_dump(mode="json") for s in items],
        "total": len(items),
    }


@triggers_router.get(
    "/{trigger_id}/subscriptions/{subscription_id}",
    summary="Get a subscription",
)
async def get_subscription_endpoint(
    trigger_id: str = Path(...),
    subscription_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    try:
        sub = await get_subscription(
            trigger_id=trigger_id,
            subscription_id=subscription_id,
            deps=deps,
        )
    except SubscriptionNotFound as exc:
        _raise_code(404, "subscription_not_found", str(exc))
    return JSONResponse(
        status_code=200,
        content=sub.model_dump(mode="json"),
    )


@triggers_router.put(
    "/{trigger_id}/subscriptions/{subscription_id}",
    summary="Update a subscription",
)
async def update_subscription_endpoint(
    body: SubscriptionUpdateBody,
    trigger_id: str = Path(...),
    subscription_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    # Build kwargs respecting fields the client actually supplied so a
    # missing body key doesn't clobber an existing value with None.
    sent = body.model_fields_set
    kwargs: dict[str, Any] = {}
    if "payload_template" in sent:
        kwargs["payload_template"] = body.payload_template
    if "parallelism" in sent:
        kwargs["parallelism"] = body.parallelism
    if "enabled" in sent:
        kwargs["enabled"] = body.enabled
    if "description" in sent:
        kwargs["description"] = body.description
    try:
        sub = await update_subscription(
            trigger_id=trigger_id,
            subscription_id=subscription_id,
            deps=deps,
            **kwargs,
        )
    except SubscriptionNotFound as exc:
        _raise_code(404, "subscription_not_found", str(exc))
    return JSONResponse(
        status_code=200,
        content=sub.model_dump(mode="json"),
    )


@triggers_router.delete(
    "/{trigger_id}/subscriptions/{subscription_id}",
    summary="Delete a subscription",
)
async def delete_subscription_endpoint(
    trigger_id: str = Path(...),
    subscription_id: str = Path(...),
    sp=Depends(get_storage_provider),
    claim_engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> JSONResponse:
    deps = _deps(sp, claim_engine, event_bus)
    try:
        await delete_subscription(
            trigger_id=trigger_id,
            subscription_id=subscription_id,
            deps=deps,
        )
    except SubscriptionNotFound as exc:
        _raise_code(404, "subscription_not_found", str(exc))
    return JSONResponse(status_code=204, content=None)


__all__ = ["triggers_router"]
