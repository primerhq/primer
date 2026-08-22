"""Platform event-log read window + event-subscription management.

Spec: ``docs/superpowers/specs/2026-08-22-event-bus-design.md``.

* ``GET /v1/events`` - cursor-paginated read of the durable log (the
  debugging window; the id is the cursor).
* CRUD ``/v1/event_subscriptions`` - user-defined subscriptions.
  ``converge`` sinks are system-only; seeded rows (managed_by
  "system") reject update/delete but expose a ``paused`` toggle.
"""

from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_event_subscription_storage,
    get_storage_provider,
)
from primer.api.routers._crud import make_crud_router
from primer.api.errors import common_responses
from primer.model.event import Event, EventSubscription
from primer.model.except_ import (
    NotFoundError,
    ValidationError as SemanticValidationError,
)

events_router = APIRouter(tags=["events"])


class EventListResponse(BaseModel):
    items: list[Event]
    max_id: int = Field(
        ..., description="Highest id in the log (0 when empty); poll "
        "with after_id=max_id for a tail.",
    )


@events_router.get(
    "/events",
    response_model=EventListResponse,
    summary="Read the platform event log",
    responses=common_responses(500),
)
async def list_events(
    after_id: int = Query(0, ge=0, description="Return events with id above this."),
    limit: int = Query(100, ge=1, le=500),
    event_type: str | None = Query(
        None, description="Event-type glob, e.g. 'agent.*'."),
    entity_kind: str | None = Query(None),
    entity_id: str | None = Query(None),
    workspace_id: str | None = Query(None),
    since: datetime | None = Query(None),
    storage_provider=Depends(get_storage_provider),
) -> EventListResponse:
    store = storage_provider.get_event_store()
    prefix = None
    if event_type:
        # Push the literal head of the glob into SQL; finish in Python.
        head = min(
            (event_type.find(c) for c in "*?[" if c in event_type),
            default=len(event_type),
        )
        prefix = event_type[:head] or None
    items = await store.read_after(
        after_id, limit=limit, event_type_prefix=prefix,
        entity_kind=entity_kind, entity_id=entity_id,
        workspace_id=workspace_id, since=since,
    )
    if event_type:
        items = [e for e in items if fnmatchcase(e.event_type, event_type)]
    return EventListResponse(items=items, max_id=await store.max_id())


class _PausedBody(BaseModel):
    paused: bool


@events_router.post(
    "/event_subscriptions/{subscription_id}/paused",
    summary="Pause or resume an event subscription",
    responses=common_responses(404, 500),
)
async def set_subscription_paused(
    request: Request,
    subscription_id: str = Path(...),
    body: _PausedBody = Body(...),
    storage=Depends(get_event_subscription_storage),
) -> EventSubscription:
    """The one mutation allowed on system-managed subscriptions."""
    row = await storage.get(subscription_id)
    if row is None:
        raise NotFoundError(
            f"event subscription {subscription_id!r} does not exist"
        )
    return await storage.update(row.model_copy(update={"paused": body.paused}))


async def _reject_converge_sink(
    entity: EventSubscription, request: Request,
) -> None:
    if entity.sink.kind == "converge":
        raise SemanticValidationError(
            "converge sinks are system-managed; user subscriptions may "
            "use 'log' (session_wake belongs to the wait_for_event tool)"
        )


event_subscription_router = make_crud_router(
    model_cls=EventSubscription,
    storage_dep=get_event_subscription_storage,
    plural="event_subscriptions",
    tag="event-subscriptions",
    managed_by_field="managed_by",
    on_pre_create=_reject_converge_sink,
)


__all__ = ["events_router", "event_subscription_router"]
