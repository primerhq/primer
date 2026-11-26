"""Platform event-log read window + event-subscription management.

Spec: ``docs/superpowers/specs/2026-08-22-event-bus-design.md``.

* ``GET /v1/events`` - cursor-paginated read of the durable log (the
  debugging window; the id is the cursor).
* CRUD ``/v1/event_subscriptions`` - user-defined subscriptions.
  ``converge`` sinks are system-only; seeded rows (managed_by
  "system") reject update/delete but expose a ``paused`` toggle.
"""

from __future__ import annotations

from primer.api.route_tools import route_tool

from datetime import datetime
from fnmatch import fnmatchcase

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from primer.access.person import person_from_request
from primer.access.workspaces import require_workspace
from primer.api.deps import (
    get_event_subscription_storage,
    get_storage_provider,
    require_admin,
    require_user,
)
from primer.api.routers._crud import make_crud_router
from primer.api.route_tools import CrudToolDeclaration
from primer.api.errors import common_responses
from primer.events.filters import validate_filter
from primer.events.redaction import redact_event
from primer.events.visibility import user_safe, without_input
from primer.model.access import AccessLevel
from primer.model.event import Event, EventSubscription
from primer.model.except_ import (
    NotFoundError,
    ValidationError as SemanticValidationError,
)

events_router = APIRouter(tags=["events"])

# How many rows one GET /v1/events may scan while filling the page past the
# Python-side filters (the glob tail, the non-admin visibility): the house
# defensive walk cap.
_SCAN_CAP = 10_000

# A backward read (``before_id``) walks the id space downward in windows of this many ids, one query each, and stops after
# ``_BACK_MAX_WINDOWS`` windows even when the page is not full: it then hands back ``next_before_id`` and the caller asks again, so one
# request stays bounded however sparse the filter is.
_BACK_WINDOW = 2_000
_BACK_MAX_WINDOWS = 20

# What a non-admin may see of the log (their workspace's operational
# lifecycle, never entity CRUD) is ``user_safe``, checked against the
# redacted stream, and a canvas.event reaches them without what the user
# entered (``without_input``: its ``value`` and ``data``, whoever's session
# it is; audit G8-08). Both live in ``primer.events.visibility``, which the
# ``wait_for_event`` tool and the dispatcher's session wake read too (a
# wake drops the input for every run, an admin's included).


class EventListResponse(BaseModel):
    items: list[Event]
    max_id: int = Field(
        ..., description="Highest id in the log (0 when empty); poll "
        "with after_id=max_id for a tail.",
    )
    next_before_id: int | None = Field(
        None, description="Only on a before_id read: pass it as before_id for the next older page; "
        "null when nothing older is left to read.",
    )


@events_router.get(
    "/events",
    response_model=EventListResponse,
    summary="Read the platform event log",
    responses=common_responses(500),
)
@route_tool(scoped_id="events__list_events")
async def list_events(
    request: Request,
    after_id: int = Query(0, ge=0, description="Return events with id above this."),
    before_id: int | None = Query(
        None, ge=1,
        description="Read backwards: the newest events with id below this (the filters applied), in id order. "
        "A page may come back short with next_before_id set when the bounded scan ran out; ask again with it. Not with after_id.",
    ),
    limit: int = Query(100, ge=1, le=500),
    event_type: str | None = Query(
        None, description="Event-type glob, e.g. 'agent.*'."),
    entity_kind: str | None = Query(None),
    entity_id: str | None = Query(None),
    workspace_id: str | None = Query(None),
    since: datetime | None = Query(None),
    storage_provider=Depends(get_storage_provider),
    user=Depends(require_user),
) -> EventListResponse:
    # Non-admins read a WORKSPACE feed, not the platform log: the
    # workspace filter is mandatory and only operational lifecycle
    # kinds come back (P6 T14; the console's events sidebar is the
    # consumer).
    is_admin = user is None or getattr(user, "role", "admin") == "admin"
    if not is_admin and not workspace_id:
        raise SemanticValidationError(
            "non-admin event reads must name a workspace_id"
        )
    if not is_admin:
        # The named workspace must be one the caller may read: an id
        # that is missing or out of access answers like a missing row
        # (spec 9). Admins read any feed.
        await require_workspace(
            storage_provider, person_from_request(request),
            workspace_id, AccessLevel.READ,
        )
    if before_id is not None and after_id:
        raise SemanticValidationError("before_id and after_id cannot be combined")
    store = storage_provider.get_event_store()
    prefix = None
    if event_type:
        # Push the literal head of the glob into SQL; finish in Python.
        head = min(
            (event_type.find(c) for c in "*?[" if c in event_type),
            default=len(event_type),
        )
        prefix = event_type[:head] or None
    if before_id is not None:
        return await _read_back(
            store, before_id, limit=limit, is_admin=is_admin, event_type=event_type, prefix=prefix,
            entity_kind=entity_kind, entity_id=entity_id, workspace_id=workspace_id, since=since,
        )
    # Read until the page has ``limit`` rows that pass the Python-side filters
    # (the glob tail, the non-admin visibility) or the log runs out: a page
    # whose first rows are all filtered must not starve the reader. Bounded
    # defensively at _SCAN_CAP rows scanned.
    kept: list[Event] = []
    after = after_id
    seen = after_id
    scanned = 0
    while len(kept) < limit and scanned < _SCAN_CAP:
        page = await store.read_after(
            after, limit=limit, event_type_prefix=prefix,
            entity_kind=entity_kind, entity_id=entity_id,
            workspace_id=workspace_id, since=since,
        )
        if not page:
            break
        after = seen = max(seen, page[-1].id)
        scanned += len(page)
        kept.extend(_visible(page, is_admin, event_type))
        if len(page) < limit:
            break
    # A non-admin's head is the newest event of the readable workspace they
    # named (the rows read above), never the global log's (which counts
    # unreadable workspaces).
    max_id = await store.max_id() if is_admin else (seen if scanned else after_id)
    # EVERY read is redacted, the admin's included: the gate is access
    # control, not an excuse to ship stored secrets to a browser.
    items = [redact_event(e) for e in kept[:limit]]
    return EventListResponse(items=items, max_id=max_id)


def _visible(page: list[Event], is_admin: bool, event_type: str | None) -> list[Event]:
    """The rows of a store page that pass the Python-side filters (the glob tail, the non-admin visibility)."""
    if is_admin:
        return [e for e in page if not event_type or fnmatchcase(e.event_type, event_type)]
    # The feed is scoped to a workspace the caller can read, whose sessions they may all open, so it does not scope events to sessions;
    # it drops the INPUT of a canvas interaction, which never leaves its session (audit G8-08). An admin reads the full event: the one
    # door to another session's input.
    return [without_input(e) for e in page if user_safe(e.event_type)]


async def _read_back(
    store, before_id: int, *, limit: int, is_admin: bool, event_type: str | None, prefix: str | None,
    entity_kind: str | None, entity_id: str | None, workspace_id: str | None, since: datetime | None,
) -> EventListResponse:
    """The newest ``limit`` matching events below ``before_id``, in id order, read window by window (the store only reads forward)."""
    head = await store.max_id()
    hi = min(before_id, head + 1)  # exclusive upper bound; ids above the log's head hold nothing
    kept: list[Event] = []
    windows = 0
    while hi > 1 and len(kept) < limit and windows < _BACK_MAX_WINDOWS:
        lo = max(0, hi - 1 - _BACK_WINDOW)  # the window is the ids in (lo, hi): at most _BACK_WINDOW of them
        page = await store.read_after(
            lo, limit=_BACK_WINDOW + 1, event_type_prefix=prefix, entity_kind=entity_kind, entity_id=entity_id,
            workspace_id=workspace_id, since=since,
        )
        kept = _visible([e for e in page if e.id < hi], is_admin, event_type) + kept
        hi = lo + 1  # everything from lo + 1 up has been read
        windows += 1
    if len(kept) >= limit:
        kept = kept[-limit:]
        nxt = kept[0].id if kept else None
    else:
        nxt = hi if hi > 1 else None
    if nxt is not None and nxt <= 1:
        nxt = None
    # A non-admin's head is the newest event of the readable workspace they named, never the global log's.
    max_id = head if is_admin else (kept[-1].id if kept else 0)
    return EventListResponse(items=[redact_event(e) for e in kept], max_id=max_id, next_before_id=nxt)


class _PausedBody(BaseModel):
    paused: bool


@events_router.post(
    "/event_subscriptions/{subscription_id}/paused",
    summary="Pause or resume an event subscription",
    responses=common_responses(403, 404, 500),
    # Event subscriptions are admin-only (access-control spec decision 3): pausing system-cdc or another user's wait is not a user act.
    dependencies=[Depends(require_admin)],
)
@route_tool(scoped_id="events__set_subscription_paused")
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
    _reject_unsafe_expr(entity)
    if entity.sink.kind == "converge":
        raise SemanticValidationError(
            "converge sinks are system-managed; user subscriptions may "
            "use 'log' (session_wake belongs to the wait_for_event tool)"
        )


def _reject_unsafe_expr(entity: EventSubscription) -> None:
    """The expr check and the field-regex check (G6-11: the matching engine, at most 1000 characters)."""
    try:
        validate_filter(entity.filter)
    except ValueError as exc:
        raise SemanticValidationError(str(exc)) from exc


async def _reject_unsafe_expr_on_update(
    entity: EventSubscription,
    existing: EventSubscription,
    request: Request,
) -> None:
    _reject_unsafe_expr(entity)


event_subscription_router = make_crud_router(
    model_cls=EventSubscription,
    storage_dep=get_event_subscription_storage,
    plural="event_subscriptions",
    tool_declaration=CrudToolDeclaration(
        resource_id="event_subscriptions",
        generated_operations=frozenset({"create", "get", "update", "delete", "list", "find"}),
    ),
    tag="event-subscriptions",
    managed_by_field="managed_by",
    on_pre_create=_reject_converge_sink,
    on_pre_update=_reject_unsafe_expr_on_update,
)


__all__ = ["events_router", "event_subscription_router"]
