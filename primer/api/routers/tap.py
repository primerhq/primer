"""Workspace tap — read-only SSE stream of :class:`TapEvent` frames.

Spec: ``docs/superpowers/specs/2026-06-30-workspace-tap-design.md`` §2.2,
§2.3, §3, §8. Plan: ``docs/superpowers/plans/2026-06-30-workspace-tap.md``
Task 2.1.

``GET /v1/workspaces/{workspace_id}/tap`` opens a Server-Sent Events stream.
It is the first real surface over the Phase-1 tap spine and validates it
end-to-end:

* **Auth** is the cookie auth of the rest of the API: the auth middleware
  populates ``request.state.user`` from the signed ``primer_session`` cookie;
  we reject with HTTP 401 when it is absent (the HTTP analogue of the
  terminal WebSocket's 4401 close).
* **Selector** arrives as an optional ``?selector=`` query parameter carrying a
  base64url- or raw-JSON-encoded :class:`TapSelector`. A GET stream has no
  body, so a query parameter keeps the surface cacheable, reconnect-friendly,
  and trivial for any client to build: append ``?selector=``.
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
from collections import deque
from datetime import datetime, timezone
import base64
import binascii
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from primer.access.person import person_from_request
from primer.access.workspaces import WorkspaceAccessWatch, require_workspace
from primer.model.access import AccessLevel
from primer.model.except_ import AccessDeniedError, BadRequestError, NotFoundError
from primer.model.storage import FieldRef, Op, OffsetPage, Predicate, Value
from primer.model.user import User
from primer.model.workspace_session import WorkspaceSession
from primer.tap.cursor import _EPOCH, TapCursor
from primer.tap.event import TapEvent, TapEventClass
from primer.tap.reader import agent_graph_ids, read_session_since
from primer.tap.router import WorkspaceTapRouter, WorkspaceTick
from primer.tap.selector import (
    TapSelector,
    TapSelectorError,
    TapSelectorTimeout,
    check_selector,
    pinned_session_ids,
    session_predicate_for_storage,
)

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
    """Decode the ``?selector=`` parameter and check its LIKE patterns (G6-4); either failure is a 400."""
    selector = _parse_selector(raw)
    try:
        check_selector(selector)
    except TapSelectorError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "/errors/invalid-selector",
                "title": "Unusable tap selector",
                "detail": str(exc),
            },
        ) from None
    return selector


def _parse_selector(raw: str | None) -> TapSelector:
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


# The reconnect delay (ms) an error frame asks for: an EventSource reconnects
# on its own after a stream ends, and the same selector would only fail again.
_ERROR_RETRY_MS = 3_600_000


def _error_frame(type_: str, detail: str) -> str:
    """A named ``error`` SSE event (no ``id:``: it never moves the cursor), with a long ``retry:`` (round 2, L4)."""
    data = json.dumps({"type": type_, "detail": detail})
    return f"retry: {_ERROR_RETRY_MS}\nevent: error\ndata: {data}\n\n"


def _delta_frame(frame: dict) -> str:
    """Render one ephemeral delta frame as a bare SSE ``data:`` line.

    No ``id:`` line: deltas never advance the tap cursor (the durable tick is
    the only cursor pointer), so a reconnect resumes from the durable log,
    not from deltas. An old client that keys off ``class`` and ``seq`` sees a
    frame with neither and ignores it (the existing seq guard drops it).
    """
    return f"data: {json.dumps(frame)}\n\n"


#: How many undelivered delta frames one SSE stream may hold. Deltas are ephemeral (the durable log is the record), so a slow client loses the OLDEST
#: first; ticks are never dropped (they are pointers into that log).
MAX_PENDING_DELTAS = 1000


class _TapInbox:
    """What the SSE loop waits on: arrival-ordered ticks and deltas, ticks coalesced per session, deltas bounded.

    A tick only says "read this session's log up to the end", so a tick already waiting for a session is replaced IN PLACE by its newer tick (one read
    covers both). Past :data:`MAX_PENDING_DELTAS` waiting deltas the oldest delta is dropped. Memory is therefore bounded by the sessions of the
    workspace plus the delta bound, whatever the client does.
    """

    def __init__(self) -> None:
        self._entries: dict[int, tuple[str, Any]] = {}  # insertion-ordered: arrival order
        self._tick_slot: dict[str, int] = {}  # session id -> key of its waiting tick
        self._delta_keys: deque[int] = deque()
        self._next = 0
        self._wake = asyncio.Event()

    def pending(self) -> int:
        return len(self._entries)

    def put_tick(self, tick: Any) -> None:
        slot = self._tick_slot.get(tick.session_id)
        if slot is not None and slot in self._entries:
            self._entries[slot] = ("tick", tick)
        else:
            self._tick_slot[tick.session_id] = self._add(("tick", tick))
        self._wake.set()

    def put_delta(self, frame: dict) -> None:
        self._delta_keys.append(self._add(("delta", frame)))
        while len(self._delta_keys) > MAX_PENDING_DELTAS:
            self._entries.pop(self._delta_keys.popleft(), None)
        self._wake.set()

    def _add(self, entry: tuple[str, Any]) -> int:
        key = self._next
        self._next += 1
        self._entries[key] = entry
        return key

    async def get(self) -> tuple[str, Any]:
        while not self._entries:
            self._wake.clear()
            await self._wake.wait()
        key = next(iter(self._entries))
        tag, item = self._entries.pop(key)
        if tag == "tick":
            if self._tick_slot.get(item.session_id) == key:
                del self._tick_slot[item.session_id]
        elif self._delta_keys and self._delta_keys[0] == key:
            self._delta_keys.popleft()
        else:
            try:
                self._delta_keys.remove(key)
            except ValueError:
                pass
        return tag, item


def _aware(moment: datetime) -> datetime:
    """``moment`` as a timezone-aware UTC time (a client-built cursor may carry a naive one)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _needs_catch_up(row: WorkspaceSession, cursor: TapCursor, baseline: dict[str, int], since: datetime | None) -> bool:
    """Whether a reconnecting stream must read ``row``'s log once before any tick: it gained records while the client was away, or its turn is running.

    A session the cursor never named is only read when it had activity since the cursor's ``known_as_of``, so a workspace of idle sessions costs no reads.
    """
    if getattr(row, "turn_status", None) == "running":
        return True
    resume = cursor.seqs[row.id] if row.id in cursor.seqs else baseline.get(row.id, 0)
    if row.last_seq <= resume:
        return False
    if since is not None and row.id not in cursor.seqs:
        active = getattr(row, "last_activity_at", None)
        return active is None or _aware(active) > since
    return True


async def _stream_tap(
    *,
    router: WorkspaceTapRouter,
    sessions_storage: Any,
    workspace_io: Any,
    workspace_id: str,
    selector: TapSelector,
    cursor: TapCursor,
    had_cursor: bool,
    watch: WorkspaceAccessWatch | None = None,
    initial_rows: list[WorkspaceSession] | None = None,
):
    """Async generator yielding SSE frames for a workspace tap.

    ``watch`` re-checks the person's level (final access review F8) before every batch (a tick) and keepalive, and at most once a
    second between deltas: once it fails, the stream sends the refusal a fresh connect would get as an error frame and ends.

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

    # Subscribe BEFORE the rows are read: a tick landing while they load waits in the inbox instead of vanishing (W27).
    tick_sub = router.subscribe(workspace_id)
    delta_sub = router.subscribe_delta(workspace_id)
    inbox = _TapInbox()

    async def _pump(sub, put):
        try:
            async for item in sub:
                put(item)
        except asyncio.CancelledError:
            pass
        except StopAsyncIteration:
            pass
        except Exception:
            logger.exception("workspace tap: pump failed")

    pump_tick = asyncio.create_task(_pump(tick_sub, inbox.put_tick))
    pump_delta = asyncio.create_task(_pump(delta_sub, inbox.put_delta))
    loop = asyncio.get_running_loop()
    checked_at = float("-inf")
    pinned = pinned_session_ids(selector)

    async def _denied() -> str | None:
        """The refusal frame once the person's level no longer holds, else ``None`` (F8: checked before every batch that yields)."""
        if watch is None:
            return None
        try:
            await watch.check()
        except AccessDeniedError as exc:
            return _error_frame("/errors/forbidden", str(exc))
        except NotFoundError as exc:
            return _error_frame("/errors/not-found", str(exc))
        return None

    try:
        # A reconnect re-reads the rows now that the subscription is live; a fresh connect can use the ones the route resolved to validate the predicate.
        rows = initial_rows if initial_rows is not None and not had_cursor else await _resolve_in_scope(
            sessions_storage, workspace_id=workspace_id, selector=selector
        )
        connected_at = datetime.now(timezone.utc)
        # What a session absent from the cursor resumes after: nothing for a cursor this server issued (its ``known_as_of`` gates the records, W28),
        # the row's own high-water mark for a fresh connect or a cursor built by a client (live-from-now; such a cursor names only what it observed).
        since: datetime | None = None
        baseline: dict[str, int] = {}
        if had_cursor and _aware(cursor.known_as_of) > _aware(_EPOCH):
            since = _aware(cursor.known_as_of)
        else:
            baseline = {row.id: row.last_seq for row in rows if row.id not in cursor.seqs}
            cursor.known_as_of = connected_at
        for row in rows:
            in_scope[row.id] = (row, 0)
            if had_cursor and _needs_catch_up(row, cursor, baseline, since):
                inbox.put_tick(WorkspaceTick(session_id=row.id, seq=row.last_seq))

        while True:
            try:
                tag, item = await asyncio.wait_for(
                    inbox.get(), timeout=_KEEPALIVE_INTERVAL_S
                )
            except TimeoutError:
                tag, item = "keepalive", None
            if tag == "keepalive":
                if (refusal := await _denied()) is not None:
                    yield refusal
                    return
                # Idle: emit a keepalive comment so proxies do not reap us.
                yield ": keepalive\n\n"
                continue

            if tag == "delta":
                # A stream scoped by a sessions predicate shows only the deltas of the sessions it resolved (W57b).
                if selector.sessions is not None and item.get("session_id") not in in_scope:
                    continue
                if watch is not None and loop.time() - checked_at >= 1.0:
                    checked_at = loop.time()
                    if (refusal := await _denied()) is not None:
                        yield refusal
                        return
                yield _delta_frame(item)
                continue

            wtick = item
            sid = wtick.session_id
            entry = in_scope.get(sid)
            if entry is None:
                if pinned is not None and sid not in pinned:
                    continue  # the predicate pins other ids: no query can put this session in scope (W30)
                row = await _resolve_single_in_scope(
                    sessions_storage,
                    workspace_id=workspace_id,
                    session_id=sid,
                    selector=selector,
                )
                if row is None:
                    continue
                in_scope[sid] = (row, 0)
                entry = in_scope[sid]

            row, byte_offset = entry
            try:
                events, next_offset = await read_session_since(
                    workspace_io,
                    workspace_id=workspace_id,
                    session=row,
                    after_seq=cursor.seqs[sid] if sid in cursor.seqs else baseline.get(sid, 0),
                    selector=selector,
                    from_offset=byte_offset,
                )
            except TapSelectorTimeout as exc:
                # The selector's LIKE patterns ran out of one read's time
                # budget (G6-4): say so in a named error frame and end the
                # stream, rather than dropping events silently or crashing.
                yield _error_frame("/errors/selector-timeout", str(exc))
                return
            in_scope[sid] = (row, next_offset)
            if since is not None and sid not in cursor.seqs:
                events = [ev for ev in events if _aware(ev.ts) > since]

            if events and (refusal := await _denied()) is not None:
                yield refusal
                return
            rebound: tuple[str | None, str | None] | None = None
            for ev in events:
                if ev.class_ is TapEventClass.AGENT_MARKER:
                    # A binding switch: the row held since connect names the old agent, so frames from here on carry the fresh one (W57e).
                    try:
                        fresh = await sessions_storage.get(sid)
                    except Exception:  # noqa: BLE001 - a frame with the old binding beats a dead stream
                        logger.exception("workspace tap: re-reading session %s after its agent marker failed", sid)
                        fresh = None
                    if fresh is not None:
                        row = fresh
                        in_scope[sid] = (row, next_offset)
                        rebound = agent_graph_ids(row)
                if rebound is not None:
                    ev.agent_id, ev.graph_id = rebound
                cursor.advance(sid, ev.seq)
                ev.cursor = cursor.encode()
                yield _frame(ev)
    finally:
        await tick_sub.aclose()
        await delta_sub.aclose()
        pump_tick.cancel()
        pump_delta.cancel()


# ---------------------------------------------------------------------------
# Derived envelopes
# ---------------------------------------------------------------------------
#
# This is a frame, not a record. It is built from the session log,
# never advances the tap cursor, and carries STATE rather than deltas,
# so a client that reconnects re-derives the current snapshot instead
# of replaying a historical one, and one that receives the same frame
# twice renders it twice with no effect.


def build_usage_frame(raw_lines: list[str]) -> dict:
    """Token totals, turns and model calls for what the session currently shows (see ``SessionUsage``)."""
    from primer.session.usage import session_usage

    usage = session_usage(raw_lines)
    return {
        "turns": usage.turns,
        "model_calls": usage.model_calls,
        "last_input_tokens": usage.last_input_tokens,
        "last_output_tokens": usage.last_output_tokens,
        "total_input_tokens": usage.total_input_tokens,
        "total_output_tokens": usage.total_output_tokens,
        "total_cached_input_tokens": usage.total_cached_input_tokens,
        "total_reasoning_tokens": usage.total_reasoning_tokens,
    }


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
    # RBAC role gate (auth plan Task 8): the tap streams live workspace
    # activity, so it requires at least the ``user`` role. A ``restricted``
    # account authenticates but may NOT subscribe — 403 forbidden_role (the
    # HTTP analogue of the session WS 4403 close, mirroring the 401 above and
    # the include-time require_user dependency).
    if user.role not in ("user", "admin"):
        raise HTTPException(status_code=403, detail={"error": "forbidden_role"})

    app_state = request.app.state
    # Access (P2 Task 2): the tap streams live workspace activity, so it
    # needs READ on the workspace. A stranger answers the missing-workspace
    # 404 before anything about the stream is confirmed.
    person = person_from_request(request)
    await require_workspace(app_state.storage_provider, person, workspace_id, AccessLevel.READ)
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
    had_cursor = bool(tap_cursor.seqs) or _aware(tap_cursor.known_as_of) > _aware(_EPOCH)

    # The sessions predicate is resolved BEFORE the response starts: a predicate the store refuses is a 400 the client can read, not a stream that
    # dies after its 200 and is reopened every few seconds (W26).
    try:
        initial = await _resolve_in_scope(sessions_storage, workspace_id=workspace_id, selector=parsed_selector)
    except BadRequestError as exc:
        raise HTTPException(
            status_code=400,
            detail={"type": "/errors/invalid-selector", "title": "Unusable tap selector", "detail": str(exc)},
        ) from None

    generator = _stream_tap(
        initial_rows=initial,
        router=router,
        sessions_storage=sessions_storage,
        workspace_io=workspace_io,
        workspace_id=workspace_id,
        selector=parsed_selector,
        cursor=tap_cursor,
        had_cursor=had_cursor,
        watch=WorkspaceAccessWatch(storage_provider, person, workspace_id, AccessLevel.READ),
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
