"""Workspace tap — read-only SSE stream of :class:`TapEvent` frames.

Spec: ``docs/superpowers/specs/2026-06-30-workspace-tap-design.md`` §2.2,
§2.3, §3, §8. Plan: ``docs/superpowers/plans/2026-06-30-workspace-tap.md``
Task 2.1.

``GET /v1/workspaces/{workspace_id}/tap`` opens a Server-Sent Events stream.
It is the first real surface over the Phase-1 tap spine and validates it
end-to-end:

* **Auth** mirrors the session WS: the auth middleware populates
  ``request.state.user`` from the signed ``primer_session`` cookie; we reject
  with HTTP 401 when it is absent (the HTTP analogue of the WS 4401 close).
* **Selector** arrives as an optional ``?selector=`` query parameter carrying a
  base64url- or raw-JSON-encoded :class:`TapSelector`. A GET stream has no
  body, so a query parameter keeps the surface cacheable, reconnect-friendly,
  and CLI-trivial (``primectl tap`` just appends ``?selector=``).
* **Cursor / reconnect** comes from the ``Last-Event-ID`` request header
  (SSE-native, set automatically by browsers/`httpx-sse` on reconnect) and
  falls back to ``?cursor=``; the header wins. Both decode via the tolerant
  :meth:`TapCursor.decode`.
* **Live-from-connect:** with NO cursor the stream starts from each in-scope
  session's current ``last_seq`` high-water mark, so history is not replayed.
  With a cursor it is used as-is for gap-free per-session catch-up.

The stream loop subscribes to ``app.state.workspace_tap_router`` and, on each
:class:`WorkspaceTick`, incrementally reads the in-scope session's durable log
via :func:`read_session_since`, advances the multi-session cursor by
``event.seq``, stamps each frame's ``id:`` with the encoded cursor token, and
yields ``id: <cursor>\\ndata: <json>\\n\\n``. Idle connections get periodic
``: keepalive`` comments so proxies do not reap them. The endpoint is strictly
read-only — controls (interrupt/approve/cancel) stay on their REST endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from primer.model.storage import FieldRef, Op, OffsetPage, Predicate, Value
from primer.model.user import User
from primer.model.workspace_session import WorkspaceSession
from primer.tap.cursor import TapCursor
from primer.tap.event import TapEvent
from primer.tap.reader import read_session_since
from primer.tap.selector import TapSelector, session_predicate_for_storage

if TYPE_CHECKING:
    from primer.tap.router import WorkspaceTapRouter

logger = logging.getLogger(__name__)

# Router is registered under the same ``/v1`` prefix + cookie-auth dependency
# as the rest of the workspace surface (see primer/api/_app_routes.py). The
# include-time ``require_auth`` dep enforces HTTP auth; we additionally read
# the principal inside the handler to fail fast + mirror the WS contract.
tap_router = APIRouter(tags=["workspace-tap"])

# Seconds of idle (no tick) before we emit an SSE keepalive comment so
# intermediary proxies do not reap an otherwise-healthy idle connection.
_KEEPALIVE_INTERVAL_S = 15.0

_ID_FIELD = "id"


def _decode_selector(raw: str | None) -> TapSelector:
    """Decode the ``?selector=`` parameter into a :class:`TapSelector`.

    Accepts either raw JSON or a base64url-encoded JSON blob (padding
    optional) so the value is safe to drop straight into a URL. A missing or
    empty value yields an empty (pass-through) selector. A present-but-invalid
    value is a client error → HTTP 400.

    Precedence: raw JSON is tried first so that a plain-JSON value that
    happens to also be valid base64url-of-valid-JSON is never mis-routed;
    base64url is only attempted when the raw text does not parse as JSON.
    """
    if raw is None or not raw.strip():
        return TapSelector()

    text = raw.strip()
    # Try raw JSON first; only fall back to base64url if that fails.
    try:
        return TapSelector.model_validate_json(text)
    except ValueError:
        pass

    # Not raw JSON — try base64url-encoded JSON.
    payload: str | None = None
    try:
        padding = (4 - len(text) % 4) % 4
        decoded = base64.urlsafe_b64decode(text + "=" * padding)
        payload = decoded.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        payload = None

    if payload is not None:
        try:
            return TapSelector.model_validate_json(payload)
        except ValueError:
            pass

    raise HTTPException(
        status_code=400,
        detail={
            "type": "/errors/invalid-selector",
            "title": "Malformed tap selector",
            "detail": "selector is neither valid JSON nor valid base64url-encoded JSON",
        },
    )


def _read_cursor(request: Request, cursor_q: str | None) -> TapCursor:
    """Resolve the reconnect cursor: ``Last-Event-ID`` header wins over query."""
    header = request.headers.get("last-event-id")
    token = header if header else cursor_q
    return TapCursor.decode(token)


async def _resolve_in_scope(
    sessions_storage: Any,
    *,
    workspace_id: str,
    selector: TapSelector,
) -> list[WorkspaceSession]:
    """Return the in-scope session rows for ``workspace_id`` under ``selector``.

    Pages through the session store with the workspace-scoped predicate built
    by :func:`session_predicate_for_storage`, exactly mirroring the reader's
    drain-path resolution.
    """
    predicate = session_predicate_for_storage(workspace_id, selector)
    rows: list[WorkspaceSession] = []
    offset = 0
    page_len = 200
    while True:
        resp = await sessions_storage.find(
            predicate, OffsetPage(offset=offset, length=page_len)
        )
        rows.extend(resp.items)
        if len(resp.items) < page_len:
            break
        offset += page_len
    return rows


async def _resolve_single_in_scope(
    sessions_storage: Any,
    *,
    workspace_id: str,
    session_id: str,
    selector: TapSelector,
) -> WorkspaceSession | None:
    """Re-resolve ONE session against ``(selector AND id == session_id)``.

    Used when a tick names a session not already in the cached in-scope set:
    a session created (or transitioned into scope) after connect is caught
    here on its first tick. Returns the row if it is in scope, else ``None``.
    """
    base = session_predicate_for_storage(workspace_id, selector)
    id_eq = Predicate(
        left=FieldRef(name=_ID_FIELD), op=Op.EQ, right=Value(value=session_id)
    )
    predicate = Predicate(left=base, op=Op.AND, right=id_eq)
    resp = await sessions_storage.find(predicate, OffsetPage(offset=0, length=1))
    return resp.items[0] if resp.items else None


def _frame(event: TapEvent) -> str:
    """Render one :class:`TapEvent` as an SSE ``id:``/``data:`` frame.

    ``event.cursor`` MUST already be stamped with the encoded multi-session
    cursor token (the resumable ``Last-Event-ID``) before calling this.
    """
    data = event.model_dump_json(by_alias=True)
    return f"id: {event.cursor}\ndata: {data}\n\n"


async def _stream_tap(
    *,
    router: "WorkspaceTapRouter",
    sessions_storage: Any,
    workspace_io: Any,
    workspace_id: str,
    selector: TapSelector,
    cursor: TapCursor,
    had_cursor: bool,
):
    """Async generator yielding SSE frames for a workspace tap.

    Maintains ``in_scope`` as ``sid -> (session_row, byte_offset)`` — only
    positive (confirmed-in-scope) entries.  On each tick for a ``sid`` NOT in
    ``in_scope``, :func:`_resolve_single_in_scope` is called to check whether
    it has entered scope; if it has, it is added and processed; if not, the tick
    is skipped WITHOUT caching the negative so a future tick re-evaluates (this
    correctly handles a session whose ``status`` transitions into scope after the
    initial snapshot). On each in-scope tick it incrementally reads the session
    log, advances the cursor by ``event.seq``, stamps each frame's cursor token,
    and yields the frame.
    """
    # sid -> (row, byte_offset); only positive entries — no negative cache.
    in_scope: dict[str, tuple[WorkspaceSession, int]] = {}

    initial = await _resolve_in_scope(
        sessions_storage, workspace_id=workspace_id, selector=selector
    )
    for row in initial:
        in_scope[row.id] = (row, 0)
        if not had_cursor:
            # Live-from-connect: jump each in-scope session to its current
            # high-water mark so pre-existing history is NOT replayed.
            cursor.advance(row.id, row.last_seq)

    sub = router.subscribe(workspace_id)
    try:
        while True:
            try:
                wtick = await asyncio.wait_for(
                    sub.__anext__(), timeout=_KEEPALIVE_INTERVAL_S
                )
            except TimeoutError:
                # Idle: emit a keepalive comment so proxies do not reap us.
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:
                return

            sid = wtick.session_id
            entry = in_scope.get(sid)
            if entry is None:
                # Session not yet confirmed in scope — re-resolve it now.
                # We do NOT cache a negative result: scope membership is
                # mutable (e.g. status transitions) so each tick re-evaluates
                # until the session enters scope or the connection closes.
                row = await _resolve_single_in_scope(
                    sessions_storage,
                    workspace_id=workspace_id,
                    session_id=sid,
                    selector=selector,
                )
                if row is None:
                    continue
                # A newly-in-scope session starts from seq 0 (full), per spec.
                in_scope[sid] = (row, 0)
                entry = in_scope[sid]

            row, byte_offset = entry
            events, next_offset = await read_session_since(
                workspace_io,
                workspace_id=workspace_id,
                session=row,
                after_seq=cursor.resume_seq(sid),
                selector=selector,
                from_offset=byte_offset,
            )
            in_scope[sid] = (row, next_offset)

            for ev in events:
                cursor.advance(sid, ev.seq)
                # STAMP: overwrite the reader's per-session placeholder with the
                # full multi-session token so each frame's id: is a resumable
                # Last-Event-ID. This is the load-bearing seam.
                ev.cursor = cursor.encode()
                yield _frame(ev)
    finally:
        await sub.aclose()


@tap_router.get("/workspaces/{workspace_id}/tap")
async def workspace_tap(
    workspace_id: str,
    request: Request,
    selector: str | None = Query(
        default=None,
        description=(
            "Optional TapSelector as base64url- or raw-JSON. Filters which "
            "sessions (``sessions`` predicate) and events (``events`` "
            "predicate) appear in the stream."
        ),
    ),
    cursor: str | None = Query(
        default=None,
        description=(
            "Optional resume cursor (opaque base64url token). The "
            "``Last-Event-ID`` request header takes precedence when present."
        ),
    ),
) -> StreamingResponse:
    """Open a read-only SSE stream of :class:`TapEvent` frames for a workspace.

    See the module docstring for the auth / selector / cursor contract.
    """
    # Auth: the include-time require_auth dep already gates HTTP routes, but we
    # read the principal explicitly to fail fast and mirror the session WS.
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise HTTPException(status_code=401, detail={"error": "auth_required"})

    app_state = request.app.state
    router = getattr(app_state, "workspace_tap_router", None)
    if router is None:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "/errors/subsystem-inactive",
                "title": "Workspace tap router not configured",
            },
        )

    storage_provider = app_state.storage_provider
    sessions_storage = storage_provider.get_storage(WorkspaceSession)

    workspace_registry = getattr(app_state, "workspace_registry", None)
    if workspace_registry is None:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "/errors/subsystem-inactive",
                "title": "Workspace registry not configured",
            },
        )
    # Resolve the live workspace IO handle (read_file + state_path) the same
    # way the session WS does. NotFoundError → 404.
    workspace_io = await workspace_registry.get_workspace(workspace_id)

    parsed_selector = _decode_selector(selector)
    tap_cursor = _read_cursor(request, cursor)
    # Resume only when the decoded cursor actually carries per-session seqs. A
    # live-from-now init always stamps the in-scope sessions' high-water marks
    # into the cursor, so any genuine reconnect token is non-empty; absent,
    # empty, OR garbage tokens all decode to no seqs and are treated as a fresh
    # connect (live-from-now), so a corrupted Last-Event-ID never dumps full
    # history.
    had_cursor = bool(tap_cursor.seqs)

    generator = _stream_tap(
        router=router,
        sessions_storage=sessions_storage,
        workspace_io=workspace_io,
        workspace_id=workspace_id,
        selector=parsed_selector,
        cursor=tap_cursor,
        had_cursor=had_cursor,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["tap_router"]
