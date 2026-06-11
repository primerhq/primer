"""REST surface for yielding-tool interactions (M3+).

Three endpoints, all rooted at ``/v1/sessions/{session_id}``:

* ``GET .../ask_user/pending`` — returns the operator-facing prompt
  payload when the session is parked on the ``ask_user`` tool. 404
  for any other state (no park, sleep park, etc.) — the panel only
  renders when the row is showing one.
* ``POST .../ask_user/respond`` — the operator's reply. Validates
  against the optional JSON Schema the tool supplied, publishes the
  reply on the event bus, returns 202 once queued.
* ``POST .../yields/{tool_call_id}/cancel`` — tool-agnostic cancel
  for a single in-flight yield. Publishes a ``YieldCancelled`` marker
  payload; the resume hook synthesises the cancelled tool result so
  the agent's turn keeps going.

The router consults the **event bus** for publish + the **session
storage** for the parked-state lookup. The bus listener (started in
the app lifespan) flips the parked row to resumable; the worker pool
picks it up via its claim loop. We do not call the scheduler
directly here — keeping that boundary clean means the same router
works against both the in-memory and the Postgres scheduler with no
code changes.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path
from pydantic import BaseModel, Field

from primer.api.deps import get_event_bus, get_session_storage
from primer.api.errors import common_responses
from primer.int.event_bus import EventBus
from primer.model.except_ import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from primer.model.workspace_session import WorkspaceSession
from primer.worker.yield_runtime import make_cancelled_payload


logger = logging.getLogger(__name__)

yields_router = APIRouter(tags=["yields"])


# ===========================================================================
# Shared lookups
# ===========================================================================


async def _load_session_or_404(session_storage, session_id: str) -> WorkspaceSession:
    sess = await session_storage.get(session_id)
    if sess is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    return sess


def _parked_blob(sess: WorkspaceSession) -> dict[str, Any] | None:
    """Return the parked_state blob if the session is parked or
    resumable, else None.

    Both states qualify: a resumable row is one whose event already
    fired but the worker hasn't claimed it yet. Either way the
    in-flight tool_call_id is the same, so the cancel-yielded-tool
    endpoint treats them uniformly. The respond endpoint, by contrast,
    only flips a row that's still ``parked`` (the atomic
    ``mark_resumable`` guards against double-respond).
    """
    if sess.parked_status not in ("parked", "resumable"):
        return None
    return sess.parked_state or None


def _tool_call_id_for(blob: dict[str, Any]) -> str | None:
    """Where to find ``tool_call_id`` inside the parked_state blob.

    Worker writes it at the top level (M3+); older parks may have
    only had it inside ``yielded.resume_metadata``. Falling back keeps
    the lookup robust if a deployment carries old parks across the
    boundary.
    """
    tcid = blob.get("tool_call_id")
    if tcid:
        return tcid
    yielded = blob.get("yielded") or {}
    metadata = yielded.get("resume_metadata") or {}
    return metadata.get("tool_call_id")


# ===========================================================================
# GET /v1/sessions/{id}/ask_user/pending
# ===========================================================================


class AskUserPendingResponse(BaseModel):
    """Operator-facing prompt payload."""

    tool_call_id: str = Field(...)
    prompt: str = Field(...)
    response_schema: dict[str, Any] | None = Field(default=None)
    parked_at: str = Field(
        ...,
        description=(
            "ISO-8601 timestamp the agent's turn parked on this "
            "prompt. UI can use it to surface a 'waiting for N "
            "seconds' affordance."
        ),
    )


@yields_router.get(
    "/sessions/{session_id}/ask_user/pending",
    response_model=AskUserPendingResponse,
    summary="Get the pending ask_user prompt (404 if none)",
    responses=common_responses(404, 500),
)
async def get_ask_user_pending(
    session_id: str = Path(...),
    session_storage=Depends(get_session_storage),
) -> AskUserPendingResponse:
    sess = await _load_session_or_404(session_storage, session_id)
    blob = _parked_blob(sess)
    if blob is None:
        raise NotFoundError(
            f"Session {session_id!r} has no pending ask_user prompt"
        )
    yielded = blob.get("yielded") or {}
    if yielded.get("tool_name") != "ask_user":
        raise NotFoundError(
            f"Session {session_id!r} is parked on a different tool"
        )
    metadata = yielded.get("resume_metadata") or {}
    tcid = _tool_call_id_for(blob)
    if not tcid:
        # A malformed park — log loudly, surface as 404 so the UI
        # doesn't loop on it.
        logger.warning(
            "ask_user park on session %s missing tool_call_id",
            session_id,
        )
        raise NotFoundError(
            f"Session {session_id!r} has a malformed ask_user park"
        )
    parked_at_iso = (
        sess.parked_at.isoformat()
        if sess.parked_at is not None
        else metadata.get("parked_at_iso", "")
    )
    return AskUserPendingResponse(
        tool_call_id=tcid,
        prompt=metadata.get("prompt", ""),
        response_schema=metadata.get("response_schema"),
        parked_at=parked_at_iso,
    )


# ===========================================================================
# POST /v1/sessions/{id}/ask_user/respond
# ===========================================================================


class AskUserRespondBody(BaseModel):
    """Operator's reply to an ask_user prompt."""

    tool_call_id: str = Field(...)
    response: Any = Field(
        ...,
        description=(
            "Operator-supplied value. May be a string, object, array, "
            "number, or boolean. Validated against the tool-supplied "
            "``response_schema`` when one was provided."
        ),
    )


def _validate_response_against_schema(
    *, response: Any, schema: dict[str, Any] | None,
) -> None:
    if schema is None:
        return
    # jsonschema arrives via mcp's transitive deps but we declare it
    # directly in pyproject for clarity.
    import jsonschema  # local import keeps the router import cheap
    from jsonschema import exceptions as jse

    try:
        jsonschema.validate(instance=response, schema=schema)
    except jse.ValidationError as exc:
        raise ValidationError(
            f"response failed schema validation: {exc.message}"
        ) from exc
    except jse.SchemaError as exc:
        # A bad schema is the tool author's bug, but we surface it as
        # a 422 so the UI can show a sensible message rather than 500.
        raise ValidationError(
            f"response_schema is invalid: {exc.message}"
        ) from exc


@yields_router.post(
    "/sessions/{session_id}/ask_user/respond",
    status_code=202,
    summary="Submit a response to a pending ask_user prompt",
    responses=common_responses(404, 422, 500),
)
async def post_ask_user_respond(
    session_id: str = Path(...),
    body: AskUserRespondBody = Body(...),
    session_storage=Depends(get_session_storage),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, str]:
    sess = await _load_session_or_404(session_storage, session_id)
    blob = _parked_blob(sess)
    if blob is None:
        raise NotFoundError(
            f"Session {session_id!r} has no pending ask_user prompt"
        )
    yielded = blob.get("yielded") or {}
    # Graph park: the outer yield is typed "_approval"; the real ask_user
    # nodes live in the checkpoint's pending_agent_yields. Match the
    # tool_call_id there and publish to that node's own event_key so a
    # graph agent-node ask_user can be answered over REST (not only the
    # channel path).
    checkpoint = blob.get("graph_checkpoint")
    if checkpoint:
        ay = next(
            (e for e in (checkpoint.get("pending_agent_yields") or [])
             if e.get("tool_call_id") == body.tool_call_id
             and e.get("tool_name") == "ask_user"),
            None,
        )
        if ay is None:
            raise NotFoundError(
                f"No pending ask_user prompt with tool_call_id "
                f"{body.tool_call_id!r} on session {session_id!r}"
            )
        ay_meta = ay.get("resume_metadata") or {}
        _validate_response_against_schema(
            response=body.response, schema=ay_meta.get("response_schema"),
        )
        ay_event_key = ay.get("event_key")
        if not ay_event_key:
            raise NotFoundError(
                f"Session {session_id!r} agent yield is missing event_key"
            )
        await event_bus.publish(ay_event_key, {"response": body.response})
        return {"status": "accepted"}
    if yielded.get("tool_name") != "ask_user":
        raise NotFoundError(
            f"Session {session_id!r} is parked on a different tool"
        )
    expected = _tool_call_id_for(blob)
    if expected != body.tool_call_id:
        raise NotFoundError(
            f"No pending ask_user prompt with tool_call_id "
            f"{body.tool_call_id!r} on session {session_id!r}"
        )
    metadata = yielded.get("resume_metadata") or {}
    _validate_response_against_schema(
        response=body.response, schema=metadata.get("response_schema"),
    )
    event_key = yielded.get("event_key")
    if not event_key:
        # Defensive — every park has one in current code.
        raise NotFoundError(
            f"Session {session_id!r} park is missing event_key"
        )
    await event_bus.publish(event_key, {"response": body.response})
    return {"status": "accepted"}


# ===========================================================================
# POST /v1/sessions/{id}/yields/{tool_call_id}/cancel
# ===========================================================================


class CancelYieldedToolBody(BaseModel):
    """Optional reason surfaced to the agent via YieldCancelled."""

    reason: str | None = Field(
        default=None,
        max_length=1024,
        description=(
            "Free-text reason the operator skipped this yield. "
            "Passed verbatim into the tool's resume hook so the agent "
            "can surface it (or react to it) in its next turn."
        ),
    )


@yields_router.post(
    "/sessions/{session_id}/yields/{tool_call_id}/cancel",
    status_code=202,
    summary="Cancel one in-flight yield (tool-agnostic)",
    responses=common_responses(404, 409, 500),
)
async def post_cancel_yielded_tool(
    session_id: str = Path(...),
    tool_call_id: str = Path(...),
    body: Annotated[
        CancelYieldedToolBody, Body(...)
    ] = CancelYieldedToolBody(),  # body is optional; default-construct
    session_storage=Depends(get_session_storage),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, str]:
    """Cancel a single yield without terminating the whole session.

    Distinct from cancel-session (§9.2 of the spec): the tool's
    resume hook IS called with a :class:`YieldCancelled` payload and
    the agent's turn continues. If the session is already terminating
    via cancel-session (``cancel_requested=True``), this endpoint
    returns 409 — there's no point asking a tool to gracefully
    continue when the session is about to die anyway.
    """
    sess = await _load_session_or_404(session_storage, session_id)
    blob = _parked_blob(sess)
    if blob is None:
        raise NotFoundError(
            f"Session {session_id!r} has no in-flight yield"
        )
    expected = _tool_call_id_for(blob)
    if expected != tool_call_id:
        raise NotFoundError(
            f"No in-flight yield with tool_call_id {tool_call_id!r} "
            f"on session {session_id!r}"
        )
    if getattr(sess, "cancel_requested", False):
        raise ConflictError(
            f"Session {session_id!r} is terminating "
            "(cancel_requested=true); can't cancel an individual yield"
        )
    yielded = blob.get("yielded") or {}
    event_key = yielded.get("event_key")
    if not event_key:
        raise NotFoundError(
            f"Session {session_id!r} park is missing event_key"
        )
    payload = make_cancelled_payload(reason=body.reason)
    await event_bus.publish(event_key, payload)
    return {"status": "accepted"}


__all__ = [
    "AskUserPendingResponse",
    "AskUserRespondBody",
    "CancelYieldedToolBody",
    "get_ask_user_pending",
    "post_ask_user_respond",
    "post_cancel_yielded_tool",
    "yields_router",
]
