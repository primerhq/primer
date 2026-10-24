"""The canvas HTTP surface (pivot P5, Tasks 13 to 15; design 8.6).

``canvas_public_router`` holds the routes the sandboxed frame loads, mounted OUTSIDE ``/v1`` with no auth dependency (the frame is a
cross-origin document, so it sends no cookies) and never in WORKER or serve-only mode:

* ``GET /canvas/frame``: the static frame shell with its own CSP (``primer.canvas.frame``);
* ``GET /canvas/sessions/{session_id}/files/{token}``: a workspace file the session's canvas shows, by capability URL
  (``primer.canvas.files``). Every refusal is the same 404, so it tells a caller nothing about which check failed.

``canvas_router`` is the console's surface, under ``/v1`` for any signed-in user (``require_user``). Every route answers 404 for a
session that does not exist or is on another workspace than the path names.

* ``GET .../canvas`` READS the canvas: the elements, the ``values`` and the newest events WITH what the users entered (a value, an
  Html emit's data). That is part of the shared session read (the user's G7-02 decision): any user who may read the session may
  read its canvas, as the lead ruled on #892 round 2 for the session's own transcript and queue, and on S6 (ruling 2) for this route.
  It never marks an event read: only the agent's reads do. The event FEED (``GET /v1/events``) is another door and still removes
  the input for a non-admin (#872).
* ``POST .../canvas/ops`` and ``DELETE .../canvas`` write it (an operator's batch, a clear), and ``POST .../canvas/events`` records a
  person's click or choice and hands it to the agent (``primer.canvas.delivery``). Each puts input into the session, which runs at
  its creator's rank, so each follows the rank rule of a steer (``refuse_outranked``: 403 ``forbidden_rank`` unless the caller ranks
  at least as high as the session's creator or the session is the caller's own; an admin is never outranked; a token acts as its
  owner), before anything is read or written. People keep their reach over every workspace.

``canvas_settings_router`` is the admin's ``GET`` and ``PUT /v1/canvas_settings``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from primer.api.routers._session_rank import refuse_outranked
from primer.api.deps import (
    get_claim_engine,
    get_event_bus,
    get_scheduler,
    get_storage_provider,
    get_workspace_registry,
)
from primer.api.errors import common_responses
from primer.canvas import files as canvas_files
from primer.canvas.decompile import MAX_EVENT_LINES, decompile
from primer.canvas.frame import build_frame_shell, frame_headers, frame_url, own_origin_of
from primer.canvas.lang import CanvasLangError, LangError, parse_program
from primer.canvas.ops import MAX_SOURCE_CHARS, CanvasOp, ensure_ops_fit, ops_errors, ops_to_statements
from primer.canvas.service import (
    CanvasApplyRefused,
    CanvasEventRefused,
    CanvasRevConflict,
    CanvasService,
)
from primer.canvas.settings import read_canvas_settings, write_canvas_settings
from primer.model.canvas import CANVAS_EVENT_KINDS, CANVAS_SETTINGS_ID, CanvasSettings, MediaPolicy, SessionCanvas
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.workspace.reserved import reserved_tree_for

canvas_public_router = APIRouter(tags=["canvas"])
canvas_router = APIRouter(tags=["canvas"])
canvas_settings_router = APIRouter(tags=["canvas"])

#: The one answer of every refused file URL.
FILE_REFUSED = "This canvas file link is not valid, or it has expired."


def _ui_dir():
    """The console's ``ui/`` (read when asked, so a test that points the console elsewhere is followed)."""
    from primer.api import _app_middleware

    return _app_middleware._UI_DIR


@canvas_public_router.get("/canvas/frame", include_in_schema=False)
async def canvas_frame(request: Request, v: str | None = Query(default=None, max_length=64)) -> Response:
    """The frame shell. Cached for a year when ``v`` is the current version; never cached otherwise."""
    shell = build_frame_shell(_ui_dir())
    return Response(
        content=shell.body,
        media_type="text/html; charset=utf-8",
        headers=frame_headers(
            version_ok=v == shell.version, own_origin=own_origin_of(request.url.scheme, request.headers.get("host")),
        ),
    )


def _file_refused() -> HTTPException:
    return HTTPException(status_code=404, detail=FILE_REFUSED)


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            "code": "canvas_file_too_large",
            "message": f"The canvas shows workspace files of at most {canvas_files.MAX_FILE_BYTES} bytes.",
        },
    )


def _file_headers(media_type: str) -> dict[str, str]:
    """A served file: for the sandboxed frame (another origin), kept private and for as long as its URL lives, never run as a page."""
    return {
        "Content-Type": media_type,
        "Cache-Control": f"private, max-age={canvas_files.FILE_URL_TTL_S}",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
        "Referrer-Policy": "no-referrer",
    }


@canvas_public_router.get("/canvas/sessions/{session_id}/files/{token}", include_in_schema=False)
async def canvas_file(
    request: Request,
    session_id: str = Path(..., max_length=256),
    token: str = Path(..., max_length=2048),
    storage_provider: Any = Depends(get_storage_provider),
    registry: Any = Depends(get_workspace_registry),
) -> Response:
    """The workspace file a capability URL names (see ``primer.canvas.files`` for the checks, in this order)."""
    verified = canvas_files.verify_file_token(
        canvas_files.canvas_file_secret(request.app.state), token, max_age=canvas_files.FILE_URL_TTL_S,
    )
    if verified is None or verified[0] != session_id:
        raise _file_refused()
    path = verified[1]
    media_type = canvas_files.media_type_of(path)
    if media_type is None or not canvas_files.safe_relative_path(path):
        raise _file_refused()
    session = await storage_provider.get_storage(WorkspaceSession).get(session_id)
    canvas = await storage_provider.get_storage(SessionCanvas).get(session_id)
    if session is None or canvas is None or path not in canvas_files.referenced_files(canvas.elements):
        raise _file_refused()
    try:
        workspace = await registry.get_workspace(session.workspace_id)
        if reserved_tree_for(workspace, path) is not None:
            raise _file_refused()
        entry = await workspace.file_info(path)
        if entry.kind != "file":
            raise _file_refused()
        if entry.size_bytes > canvas_files.MAX_FILE_BYTES:
            raise _too_large()
        raw = await workspace.read_file(path)
    except (NotFoundError, BadRequestError):
        raise _file_refused() from None
    if len(raw) > canvas_files.MAX_FILE_BYTES:
        raise _too_large()
    return Response(content=raw, media_type=media_type, headers=_file_headers(media_type))


# ===========================================================================
# The console's routes (/v1)
# ===========================================================================

_CANVAS_PATH = "/workspaces/{workspace_id}/sessions/{session_id}/canvas"
#: What an event's ``client_event_id`` may hold (a client's retry key).
_CLIENT_EVENT_ID_CHARS = 64

#: Why a route refuses a session ENDED: nothing is recorded, the click reaches no one.
SESSION_ENDED = "This session has ended, so the canvas no longer takes clicks."


class CanvasViewSettings(BaseModel):
    """What the renderer needs of the operator's settings."""

    media_policy: MediaPolicy
    media_allowlist: list[str]
    strict_mode: bool


class CanvasView(BaseModel):
    """``GET .../canvas`` (JSON): the canvas as the console renders it."""

    session_id: str
    workspace_id: str
    rev: int
    event_seq: int
    element_count: int
    elements: dict[str, dict[str, Any]]
    values: dict[str, Any]
    value_set_by: dict[str, str]
    events: list[dict[str, Any]] = Field(
        description="The newest events (at most 20), oldest first, WITH what the user entered: part of the shared session read.",
    )
    settings: CanvasViewSettings
    file_urls: dict[str, str] = Field(
        description="A capability URL for each workspace file an Image or Audio element shows, keyed `file:<path>`; each lives 900 s.",
    )
    frame_url: str = Field(description="Where the console mounts the sandboxed frame (versioned).")


class CanvasLangView(BaseModel):
    """``GET .../canvas?format=lang``: the canvas as a Canvas Lang program (no events)."""

    rev: int
    format: Literal["lang"] = "lang"
    source: str


class CanvasChanged(BaseModel):
    added: list[str]
    updated: list[str]
    removed: list[str]


class CanvasApplyResponse(BaseModel):
    """An applied batch: the new ``rev``, what changed and the warnings the batch carried."""

    rev: int
    changed: CanvasChanged
    warnings: list[str]


class CanvasOpsBody(BaseModel):
    """``POST .../canvas/ops``: Canvas Lang ``source`` or JSON ``ops`` (one of the two), optionally against ``base_rev``."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, max_length=MAX_SOURCE_CHARS, description="Canvas Lang statements, one per line.")
    ops: list[Any] | None = Field(
        default=None,
        description=(
            'JSON ops instead of source, applied in order: {op: "set", id, type, props?, children?}, {op: "prop", id, prop, value}, '
            '{op: "append", id, children}, {op: "delete", ids}, {op: "clear"}.'
        ),
    )
    base_rev: int | None = Field(default=None, ge=0, description="The rev the batch was written against; another rev is a 409.")

    @field_validator("ops", mode="before")
    @classmethod
    def _ops_fit(cls, raw: Any) -> Any:
        return ensure_ops_fit(raw)


class _Ops(BaseModel):
    """The ops of a body, judged as the tool judges them (so a malformed op is named by its number, as a bad line is)."""

    ops: list[CanvasOp]


class CanvasEventBody(BaseModel):
    """``POST .../canvas/events``: what a person did to one element. The server decides the rest (the label, whether it wakes)."""

    model_config = ConfigDict(extra="forbid")

    element: str = Field(..., min_length=1, max_length=64, description="The element's id.")
    kind: Literal[CANVAS_EVENT_KINDS] = Field(  # type: ignore[valid-type]
        ..., description="What happened: click, change, emit, set_value, play, pause, ended or open.",
    )
    value: Any = Field(default=None, description="A Select's choice (change), or an Html element's value (set_value).")
    name: str | None = Field(default=None, max_length=64, description="An Html emit's name.")
    data: Any = Field(default=None, description="An Html emit's data (at most 4096 bytes of JSON).")
    position: float | None = Field(default=None, description="An audio event's position, in seconds.")
    client_event_id: str | None = Field(
        default=None, min_length=1, max_length=_CLIENT_EVENT_ID_CHARS,
        description="The client's retry key: a repeated POST answers the event it recorded and delivers nothing again.",
    )


class CanvasEventAccepted(BaseModel):
    """A recorded event and how it reached the agent: ``wait`` (it answered a canvas_wait), ``woken`` (it woke the session),
    ``queued`` (behind the turn, or a gate), ``recorded`` (a silent event, a repeat, or a session that could not take it)."""

    event_id: str
    rev: int
    event_seq: int
    delivery: Literal["wait", "woken", "queued", "recorded"]


async def _session_on(storage_provider: Any, workspace_id: str, session_id: str) -> WorkspaceSession:
    """The session row, or 404 for one that does not exist or is on another workspace (in the words of the session routes)."""
    row = await storage_provider.get_storage(WorkspaceSession).get(session_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFoundError(f"Session {session_id!r} does not exist on workspace {workspace_id!r}")
    return row


def _service(storage_provider: Any, event_bus: Any, registry: Any) -> CanvasService:
    return CanvasService(storage_provider, event_bus=event_bus, workspace_registry=registry)


def _by(request: Request) -> str:
    """Who acts: the signed-in account's username (a token's owner); ``system`` when auth is off."""
    user = getattr(request.state, "user", None)
    return getattr(user, "username", None) or "system"


def _entries(errors: list[LangError] | tuple[LangError, ...]) -> list[dict[str, Any]]:
    """Errors as a console shows them: ``{"line" or "op": n, "message", "expected"?}``, as the tool reports them."""
    return [{e.unit: e.line, "message": e.message, **({"expected": e.expected} if e.expected else {})} for e in errors]


def _invalid(errors: list[LangError] | tuple[LangError, ...], rev: int) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "canvas_invalid", "message": f"{len(errors)} problem(s); nothing was applied", "errors": _entries(errors), "rev": rev,
        },
    )


def _applied(outcome: Any) -> CanvasApplyResponse:
    return CanvasApplyResponse(
        rev=outcome.rev,
        changed=CanvasChanged(
            added=list(outcome.changed.added), updated=list(outcome.changed.updated), removed=list(outcome.changed.removed),
        ),
        warnings=list(outcome.warnings),
    )


def _file_urls(request: Request, session_id: str, row: SessionCanvas) -> dict[str, str]:
    secret = canvas_files.canvas_file_secret(request.app.state)
    return {
        f"file:{path}": canvas_files.mint_file_url(secret, session_id, path)
        for path in sorted(canvas_files.referenced_files(row.elements))
    }


@canvas_router.get(
    _CANVAS_PATH,
    response_model=CanvasView | CanvasLangView,
    summary="Read a session's canvas",
    responses=common_responses(404, 422, 500),
)
async def get_canvas(
    request: Request,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    format: Literal["json", "lang"] = Query(default="json", description="`json` (the console's view) or `lang` (a Canvas Lang program)."),  # noqa: A002
    full: bool = Query(default=False, description="Write Html bodies out in full in the `lang` program instead of `...`."),
    storage_provider: Any = Depends(get_storage_provider),
) -> CanvasView | CanvasLangView:
    """The canvas of a session; a session without one has the empty canvas (reading never creates it, nor marks an event read).

    The JSON view carries the values and the newest events WITH what the users entered: the canvas is part of the shared session read
    (see the module docstring).
    """
    session = await _session_on(storage_provider, workspace_id, session_id)
    service = CanvasService(storage_provider)
    row = await service.snapshot(session_id, workspace_id=session.workspace_id)
    state = service.state_of(row)
    if format == "lang":
        return CanvasLangView(rev=row.rev, source=decompile(state, full=full))
    settings = await read_canvas_settings(storage_provider)
    return CanvasView(
        session_id=session_id,
        workspace_id=session.workspace_id,
        rev=row.rev,
        event_seq=row.event_seq,
        element_count=state.element_count(),
        elements=state.elements,
        values=state.values,
        value_set_by=state.value_set_by,
        events=[event.public() for event in row.events[-MAX_EVENT_LINES:]],
        settings=CanvasViewSettings(
            media_policy=settings.media_policy, media_allowlist=list(settings.media_allowlist), strict_mode=settings.strict_mode,
        ),
        file_urls=_file_urls(request, session_id, row),
        frame_url=frame_url(_ui_dir()),
    )


@canvas_router.post(
    _CANVAS_PATH + "/ops",
    response_model=CanvasApplyResponse,
    summary="Apply an operator's batch to a session's canvas",
    responses=common_responses(403, 404, 409, 422, 500),
)
async def apply_canvas_ops(
    request: Request,
    body: CanvasOpsBody,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    storage_provider: Any = Depends(get_storage_provider),
    registry: Any = Depends(get_workspace_registry),
    event_bus: Any = Depends(get_event_bus),
) -> CanvasApplyResponse:
    """Apply ``source`` or ``ops`` as one atomic batch (source ``operator``, by the caller). 422 ``canvas_invalid`` names every bad line
    or op and applies nothing; 409 ``canvas_rev_conflict`` when ``base_rev`` is stale, with the users' changes since; 403
    ``forbidden_rank`` when the session's creator outranks the caller."""
    session = await _session_on(storage_provider, workspace_id, session_id)
    refuse_outranked(request, session)
    service = _service(storage_provider, event_bus, registry)
    if (body.source is None) == (body.ops is None):
        raise HTTPException(
            status_code=422,
            detail={"code": "canvas_invalid", "message": "give source or ops, not both and not neither", "errors": []},
        )
    if body.source is not None:
        try:
            statements = await asyncio.to_thread(parse_program, body.source)
        except CanvasLangError as exc:
            raise _invalid(exc.errors, (await service.snapshot(session_id, workspace_id=session.workspace_id)).rev) from None
        unit: Literal["line", "op"] = "line"
    else:
        try:
            ops = _Ops.model_validate({"ops": body.ops}).ops
        except PydanticValidationError as exc:
            malformed = ops_errors(exc) or [LangError(1, "ops must be a list of op objects", None, "op")]
            raise _invalid(malformed, (await service.snapshot(session_id, workspace_id=session.workspace_id)).rev) from None
        statements = ops_to_statements(ops)
        unit = "op"
    try:
        outcome = await service.apply(
            session_id=session_id, workspace_id=session.workspace_id, statements=statements, unit=unit, base_rev=body.base_rev,
            source="operator", by=_by(request),
        )
    except CanvasApplyRefused as exc:
        raise _invalid(exc.errors, exc.rev) from None
    except CanvasRevConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "canvas_rev_conflict", "message": str(exc), "rev": exc.rev, "changes": exc.changes},
        ) from None
    return _applied(outcome)


@canvas_router.delete(
    _CANVAS_PATH,
    response_model=CanvasApplyResponse,
    summary="Clear a session's canvas",
    responses=common_responses(403, 404, 409, 500),
)
async def clear_canvas(
    request: Request,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    storage_provider: Any = Depends(get_storage_provider),
    registry: Any = Depends(get_workspace_registry),
    event_bus: Any = Depends(get_event_bus),
) -> CanvasApplyResponse:
    """Empty the canvas (an operator's ``clear`` batch). 403 ``forbidden_rank`` when the session's creator outranks the caller."""
    session = await _session_on(storage_provider, workspace_id, session_id)
    refuse_outranked(request, session)
    outcome = await _service(storage_provider, event_bus, registry).clear(
        session_id=session_id, workspace_id=session.workspace_id, by=_by(request),
    )
    return _applied(outcome)


@canvas_router.post(
    _CANVAS_PATH + "/events",
    response_model=CanvasEventAccepted,
    status_code=202,
    summary="Record a person's click or choice on a session's canvas",
    responses=common_responses(403, 404, 409, 413, 422, 429, 500),
)
async def post_canvas_event(
    request: Request,
    body: CanvasEventBody,
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    storage_provider: Any = Depends(get_storage_provider),
    registry: Any = Depends(get_workspace_registry),
    scheduler: Any = Depends(get_scheduler),
    engine: Any = Depends(get_claim_engine),
    event_bus: Any = Depends(get_event_bus),
) -> CanvasEventAccepted:
    """Record what a person did and deliver it to the agent by the session's state (``wait``, ``woken``, ``queued`` or ``recorded``).

    A click drives the session like a steer, so the rank rule comes first (403 ``forbidden_rank``), then an ENDED session is refused
    (409 ``session_ended``) before anything is recorded. A refused event is 422 with its ``code`` (``unknown_element``,
    ``kind_not_allowed``, ``invalid_value``, ``element_disabled``) or 429 (``rate_limited``: the waking events of a minute;
    ``silent_rate_limited``: the others), and nothing is recorded. The body is bounded before it is read (413).
    """
    from primer.canvas.delivery import DELIVERY_RECORDED, deliver_canvas_event

    session = await _session_on(storage_provider, workspace_id, session_id)
    refuse_outranked(request, session)
    if session.status == SessionStatus.ENDED:
        raise HTTPException(status_code=409, detail={"code": "session_ended", "message": SESSION_ENDED})
    service = _service(storage_provider, event_bus, registry)
    try:
        event, new = await service.record_event_or_repeat(
            session_id=session_id, workspace_id=session.workspace_id, element=body.element, kind=body.kind, by=_by(request),
            value=body.value, name=body.name, data=body.data, position=body.position, client_event_id=body.client_event_id,
        )
    except CanvasEventRefused as exc:
        status = 429 if exc.code in ("rate_limited", "silent_rate_limited") else 422
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message}) from None
    row = await service.current(session_id)
    if not new:
        delivery = DELIVERY_RECORDED                 # a retried POST: the call that recorded the event delivered it
    else:
        delivery = await deliver_canvas_event(
            event=event,
            state=service.state_of(row) if row is not None else service.state_of(SessionCanvas(id=session_id, workspace_id=workspace_id)),
            session_id=session_id,
            workspace_id=session.workspace_id,
            storage_provider=storage_provider,
            scheduler=scheduler,
            claim_engine=engine,
            workspace_registry=registry,
            event_bus=event_bus,
        )
    return CanvasEventAccepted(
        event_id=event.id, rev=row.rev if row is not None else event.rev,
        event_seq=row.event_seq if row is not None else event.seq, delivery=delivery,
    )


# ===========================================================================
# The admin's settings (/v1/canvas_settings)
# ===========================================================================


@canvas_settings_router.get(
    "/canvas_settings",
    response_model=CanvasSettings,
    summary="Read the canvas settings",
    responses=common_responses(500),
)
async def get_canvas_settings(storage_provider: Any = Depends(get_storage_provider)) -> CanvasSettings:
    """The stored canvas settings, or the defaults when an operator has saved none."""
    return await read_canvas_settings(storage_provider)


class CanvasSettingsBody(CanvasSettings):
    """``PUT /v1/canvas_settings``: every setting. A field left out takes its default; the id is always the settings row's."""

    id: str | None = Field(default=CANVAS_SETTINGS_ID, description="Ignored: there is one settings row.")


@canvas_settings_router.put(
    "/canvas_settings",
    response_model=CanvasSettings,
    summary="Replace the canvas settings",
    responses=common_responses(409, 422, 500),
)
async def put_canvas_settings(body: CanvasSettingsBody, storage_provider: Any = Depends(get_storage_provider)) -> CanvasSettings:
    """Replace the canvas settings with ``body`` (a field left out takes its default; the id is always the settings row's)."""
    settings = CanvasSettings.model_validate({**body.model_dump(exclude={"id"}), "id": CANVAS_SETTINGS_ID})
    return await write_canvas_settings(storage_provider, settings)


__all__ = [
    "FILE_REFUSED",
    "SESSION_ENDED",
    "CanvasApplyResponse",
    "CanvasEventAccepted",
    "CanvasEventBody",
    "CanvasLangView",
    "CanvasOpsBody",
    "CanvasView",
    "canvas_public_router",
    "canvas_router",
    "canvas_settings_router",
]
