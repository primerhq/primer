"""REST + WebSocket surface for the M6 chat concept.

Endpoints:

* ``POST /v1/chats`` — create a new chat bound to an agent.
* ``GET /v1/chats`` — list chats.
* ``GET /v1/chats/{id}`` — fetch a single chat.
* ``DELETE /v1/chats/{id}`` — end a chat (terminal).
* ``GET /v1/chats/{id}/messages`` — paginated message log.
* ``WS /v1/chats/{id}/ws?cursor=N`` — live message stream + send.

WebSocket protocol (server→client / client→server) is documented in
the spec §8.5; this router just enforces it. Cursor-replay on
reconnect lets a client recover from a transient disconnect without
losing messages — any rows with ``seq > cursor`` are flushed first,
then the connection takes over live streaming.

The chat executor itself is a thin stub (see
:mod:`primer.chat.executor`) for the M6 scaffold; the agent loop
that produces real assistant_token streams + tool_call dispatch
plugs in by replacing :class:`ChatTurnRunner.run_turn`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Path,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field

from primer.api.deps import get_agent_storage, get_claim_engine, get_storage_provider
from primer.api.errors import common_responses
from primer.api.pagination import parse_page
from primer.model.agent import Agent
from primer.model.chats import Chat, ChatMessage
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import (
    Op,
    OrderBy,
    PageRequest,
)
from primer.storage.q import Q
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics


logger = logging.getLogger(__name__)


chats_router = APIRouter(tags=["chats"])


# ===========================================================================
# REST: create / get / list / delete
# ===========================================================================


class ChatCreateBody(BaseModel):
    """Body of ``POST /v1/chats``."""

    agent_id: str = Field(
        ...,
        min_length=1,
        description="Agent that handles every turn of this chat.",
    )


@chats_router.post(
    "/chats",
    response_model=Chat,
    status_code=201,
    summary="Create a new chat bound to an agent",
    responses=common_responses(404, 422, 500),
)
async def create_chat(
    body: ChatCreateBody,
    sp=Depends(get_storage_provider),
    agents=Depends(get_agent_storage),
) -> Chat:
    """Allocate a new chat row + an id. Validates the agent exists."""
    agent = await agents.get(body.agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {body.agent_id!r} does not exist")
    chats_storage = sp.get_storage(Chat)
    chat = Chat(
        id=f"chat-{uuid.uuid4().hex[:12]}",
        agent_id=body.agent_id,
        created_at=datetime.now(timezone.utc),
    )
    return await chats_storage.create(chat)


@chats_router.get(
    "/chats",
    summary="List chats (paginated)",
    responses=common_responses(400, 422, 500),
)
async def list_chats(
    page: PageRequest = Depends(parse_page),
    agent_id: Annotated[
        str | None,
        Query(description="Filter by agent_id."),
    ] = None,
    sp=Depends(get_storage_provider),
):
    storage = sp.get_storage(Chat)
    if agent_id is not None:
        return await storage.find(
            Q(Chat).where("agent_id", agent_id).build(),
            page,
        )
    return await storage.list(page)


@chats_router.get(
    "/chats/{chat_id}",
    response_model=Chat,
    summary="Get a chat by id",
    responses=common_responses(404, 500),
)
async def get_chat(
    chat_id: str = Path(...),
    sp=Depends(get_storage_provider),
) -> Chat:
    storage = sp.get_storage(Chat)
    chat = await storage.get(chat_id)
    if chat is None:
        raise NotFoundError(f"Chat {chat_id!r} does not exist")
    return chat


@chats_router.delete(
    "/chats/{chat_id}",
    summary=(
        "End a chat (default) or hard-delete chat + every message "
        "(force=true)."
    ),
    responses=common_responses(404, 409, 500),
)
async def end_chat(
    chat_id: str = Path(...),
    force: bool = Query(
        False,
        description=(
            "When true, removes the chat row and every persisted "
            "chat_messages row regardless of status. The status-aware "
            "soft-end semantic does not apply: an already-ended chat "
            "deletes cleanly. Used by the operator console's 'Delete' "
            "button when the operator wants the chat gone."
        ),
    ),
    sp=Depends(get_storage_provider),
    engine=Depends(get_claim_engine),
):
    from primer.int.claim import ClaimKind

    chat_storage = sp.get_storage(Chat)
    chat = await chat_storage.get(chat_id)
    if chat is None:
        raise NotFoundError(f"Chat {chat_id!r} does not exist")

    if force:
        message_storage = sp.get_storage(ChatMessage)
        # Drain every message row for this chat. CursorPage.length is
        # capped at 200 server-side; loop until next_cursor is None.
        from primer.model.storage import CursorPage

        cursor: str | None = None
        while True:
            page = await message_storage.find(
                Q(ChatMessage).where("chat_id", chat_id).build(),
                CursorPage(cursor=cursor, length=200),
                order_by=[OrderBy(field="seq", direction="asc")],
            )
            for row in page.items:
                try:
                    await message_storage.delete(row.id)
                except NotFoundError:
                    pass
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                break
        try:
            await chat_storage.delete(chat_id)
        except NotFoundError:
            pass
        if engine is not None:
            await engine.delete_lease(ClaimKind.CHAT, chat_id)
        return {"id": chat_id, "deleted": True}

    if chat.status == "ended":
        raise ConflictError(f"Chat {chat_id!r} is already ended")
    chat.status = "ended"
    result = await chat_storage.update(chat)
    if engine is not None:
        await engine.delete_lease(ClaimKind.CHAT, chat_id)
    return result


@chats_router.get(
    "/chats/{chat_id}/messages",
    summary="List messages on a chat (paginated)",
    responses=common_responses(404, 422, 500),
)
async def list_chat_messages(
    chat_id: str = Path(...),
    after_seq: Annotated[
        int | None,
        Query(
            ge=0,
            description=(
                "Return only messages with seq > this value. Mirrors "
                "the WebSocket cursor; useful for chunked replays "
                "without holding the WS open."
            ),
        ),
    ] = None,
    page: PageRequest = Depends(parse_page),
    sp=Depends(get_storage_provider),
):
    # 404 if the chat doesn't exist so we don't leak "this id has no
    # messages" as a probe surface.
    chats_storage = sp.get_storage(Chat)
    if await chats_storage.get(chat_id) is None:
        raise NotFoundError(f"Chat {chat_id!r} does not exist")
    messages = sp.get_storage(ChatMessage)
    q = Q(ChatMessage).where("chat_id", chat_id)
    if after_seq is not None:
        q = q.where_op("seq", Op.GT, after_seq)
    return await messages.find(
        q.build(), page, order_by=[OrderBy(field="seq", direction="asc")],
    )


# ===========================================================================
# WebSocket: live stream + send
# ===========================================================================


async def _replay_since_cursor(
    ws: WebSocket,
    chat_id: str,
    cursor: int,
    sp,
) -> int:
    """Flush ``chat_messages.seq > cursor`` to ``ws`` in order.

    Returns the highest seq sent so subsequent live streaming knows
    where to resume from. Reads in chunks of 200 to keep memory
    bounded for very long chats.
    """
    from primer.model.storage import OffsetPage

    messages_storage = sp.get_storage(ChatMessage)
    cur = cursor
    PAGE = 200
    last_emitted = cur
    while True:
        pred = (
            Q(ChatMessage)
            .where("chat_id", chat_id)
            .where_op("seq", Op.GT, cur)
            .build()
        )
        page = OffsetPage(offset=0, length=PAGE)
        result = await messages_storage.find(
            pred, page, order_by=[OrderBy(field="seq", direction="asc")],
        )
        items = list(getattr(result, "items", []))
        if not items:
            break
        for item in items:
            await ws.send_json(_message_to_wire(item))
            last_emitted = item.seq
        if len(items) < PAGE:
            break
        cur = items[-1].seq
    return last_emitted


def _message_to_wire(msg: ChatMessage) -> dict[str, Any]:
    """Render a :class:`ChatMessage` for the WebSocket protocol.

    The wire envelope merges the kind-specific payload into the
    top-level object alongside ``seq`` and ``kind`` — that matches
    the spec's TypeScript union (``{ kind: ..., seq: ..., delta:
    ..., ... }``) and means clients don't need to unwrap a nested
    ``payload`` key.
    """
    out: dict[str, Any] = {"kind": msg.kind, "seq": msg.seq}
    out.update(msg.payload or {})
    return out


@chats_router.websocket("/chats/{chat_id}/ws")
async def chat_ws(
    websocket: WebSocket,
    chat_id: str,
    cursor: int = Query(0, ge=0),
) -> None:
    """Bidirectional chat stream — thin recv/send loops.

    Lifecycle:

    1. Accept the upgrade.
    2. Resolve the chat row. Reject (close 4404) if missing.
    3. Replay any chat_messages with ``seq > cursor`` in order.
    4. Subscribe to the per-chat tick router.
    5. Run two concurrent loops:
       - ``_recv_loop``: reads client frames, persists user_message rows,
         flips turn_status='claimable', publishes 'chat-claimable' bus
         events. Handles ping/interrupt/tool_approval_decide.
       - ``_send_loop``: iterates the tick subscription; on each tick
         reads new ChatMessage rows from storage and sends them as JSON.
    6. On WS disconnect: closes the tick subscription; the worker keeps
       running and will finish the in-flight turn.

    The event_bus is required for this handler — if not available the
    connection is closed with code 4500.
    """
    import time as _time
    sp = websocket.app.state.storage_provider
    # Auth check: middleware populates websocket.state.user from the
    # session cookie. Close with WS-spec code 4401 if missing.
    from primer.api.deps import require_auth_ws
    if require_auth_ws(websocket) is None:
        await websocket.accept()
        await websocket.close(code=4401, reason="auth_required")
        return

    chats_storage = sp.get_storage(Chat)
    messages_storage = sp.get_storage(ChatMessage)
    event_bus = getattr(websocket.app.state, "event_bus", None)
    chat_tick_router = getattr(websocket.app.state, "chat_tick_router", None)
    claim_engine = getattr(websocket.app.state, "claim_engine", None)

    chat = await chats_storage.get(chat_id)
    if chat is None:
        await websocket.accept()
        await websocket.close(code=4404, reason=f"chat {chat_id!r} not found")
        return
    if chat.status == "ended":
        await websocket.accept()
        await websocket.close(code=4410, reason="chat ended")
        return

    await websocket.accept()

    if event_bus is None:
        await websocket.close(code=4500, reason="event_bus_not_available")
        return

    _tracer = _tracing.get_tracer("primer.ws")
    _t0 = _time.monotonic()
    with _tracer.start_as_current_span("ws.chat") as _span:
        _metrics.ws_connections_active.labels("chat").inc()
        _frames_sent = 0
        try:
            try:
                last_seq = await _replay_since_cursor(
                    websocket, chat_id, cursor, sp,
                )
            except WebSocketDisconnect:
                return

            tick_sub = chat_tick_router.subscribe(chat_id)
            try:
                recv_task = asyncio.ensure_future(
                    _recv_loop(
                        websocket, chat_id, chats_storage, messages_storage, event_bus,
                        claim_engine=claim_engine,
                    )
                )
                send_task = asyncio.ensure_future(
                    _send_loop_instrumented(
                        websocket, chat_id, messages_storage, tick_sub, last_seq,
                        kind="chat",
                    )
                )
                try:
                    done, pending = await asyncio.wait(
                        [recv_task, send_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, WebSocketDisconnect, Exception):
                            pass
                    # Propagate exceptions from completed tasks (ignore
                    # WebSocketDisconnect which is the normal disconnect path).
                    for task in done:
                        exc = task.exception()
                        if exc is not None and not isinstance(exc, WebSocketDisconnect):
                            logger.debug(
                                "chat %s WS task raised: %s", chat_id, exc,
                            )
                except WebSocketDisconnect:
                    recv_task.cancel()
                    send_task.cancel()
                    for t in (recv_task, send_task):
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
            finally:
                await tick_sub.aclose()
        finally:
            _metrics.ws_connections_active.labels("chat").dec()
            _metrics.ws_session_duration_seconds.labels("chat").observe(
                _time.monotonic() - _t0
            )
            _span.set_attribute("ws.frames_sent", _frames_sent)


async def _recv_loop(
    websocket: WebSocket,
    chat_id: str,
    chats_storage,
    messages_storage,
    event_bus,
    *,
    claim_engine=None,
) -> None:
    """Read client frames and dispatch them.

    - ``ping`` → immediate pong.
    - ``interrupt`` → set cancel_requested_at + publish cancel event.
    - ``tool_approval_decide`` → publish on the parked event_key.
    - ``user_message`` → persist row, flip turn_status, publish claimable.
    """
    while True:
        try:
            incoming = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        kind = incoming.get("kind")
        if kind == "ping":
            await websocket.send_json({"kind": "pong"})
            continue
        if kind == "interrupt":
            chat = await chats_storage.get(chat_id)
            if chat is None or chat.status == "ended":
                continue
            # Signal the in-flight worker turn to stop.
            chat.cancel_requested_at = datetime.now(timezone.utc)
            await chats_storage.update(chat)
            await event_bus.publish(f"chat:{chat_id}:cancel", {})
            # Auto-reject any pending tool_approval park.
            await _maybe_auto_reject_pending_approval(
                chat=chat,
                event_bus=event_bus,
                note="interrupted by operator",
            )
            continue
        if kind == "tool_approval_decide":
            tcid = incoming.get("tool_call_id")
            decision = incoming.get("decision")
            reason = incoming.get("reason")
            if decision not in ("approved", "rejected"):
                await websocket.send_json({
                    "kind": "error",
                    "code": "tool_approval_bad_decision",
                    "message": f"decision must be approved/rejected; got {decision!r}",
                })
                continue
            # Re-fetch chat to get current parked_state.
            chat = await chats_storage.get(chat_id)
            if chat is None:
                continue
            blob = chat.parked_state or {}
            yielded = blob.get("yielded") or {}
            expected = (yielded.get("resume_metadata") or {}).get(
                "original_call", {},
            ).get("id")
            if expected != tcid:
                await websocket.send_json({
                    "kind": "error",
                    "code": "tool_approval_mismatch",
                    "message": "tool_call_id does not match the pending approval",
                })
                continue
            event_key = yielded.get("event_key")
            if not event_key:
                await websocket.send_json({
                    "kind": "error",
                    "code": "tool_approval_missing_event_key",
                    "message": "park is missing event_key",
                })
                continue
            await event_bus.publish(
                event_key,
                {"decision": decision, "reason": reason},
            )
            continue
        if kind != "user_message":
            await websocket.send_json(
                {
                    "kind": "error",
                    "message": f"unknown client message kind: {kind!r}",
                }
            )
            continue
        # Two payload shapes are accepted:
        #   {"content": "<text>"}           — legacy text-only
        #   {"parts": [{type, ...}, ...]}   — structured (multimodal)
        try:
            user_parts = _parse_user_message_parts(incoming)
        except ValueError as exc:
            await websocket.send_json(
                {"kind": "error", "message": str(exc)},
            )
            continue
        # Re-fetch the chat row so we have the latest last_seq.
        chat = await chats_storage.get(chat_id)
        if chat is None or chat.status == "ended":
            return
        # Auto-reject pending tool_approval park before this new turn.
        await _maybe_auto_reject_pending_approval(
            chat=chat,
            event_bus=event_bus,
            note="superseded by new user input",
        )
        # Persist the user_message row + update chat.last_seq / title.
        await _append_user_message_row(
            chat, messages_storage, chats_storage, user_parts,
        )
        # Flip turn_status to claimable and wake workers.
        latest = await chats_storage.get(chat_id)
        if latest is not None and latest.status == "active":
            latest.turn_status = "claimable"
            await chats_storage.update(latest)
            await event_bus.publish("chat-claimable", {"chat_id": chat_id})
            # Also notify the ClaimEngine (forward-compat; no-op when not wired).
            if claim_engine is not None:
                from primer.int.claim import ClaimKind
                await claim_engine.upsert(ClaimKind.CHAT, chat_id, priority=10)


async def _send_loop(
    websocket: WebSocket,
    chat_id: str,
    messages_storage,
    tick_sub,
    last_sent_seq: int,
) -> None:
    """Forward new ChatMessage rows to the WebSocket on each tick.

    Iterates the tick subscription; on each tick reads storage for rows
    with ``seq > last_sent_seq AND seq <= tick.seq`` and sends them in
    ascending order. The subscription is an AsyncIterator that blocks
    until the next tick arrives — this naturally back-pressures the WS.
    """
    from primer.model.storage import OffsetPage

    async for tick in tick_sub:
        if tick.seq <= last_sent_seq:
            continue
        pred = (
            Q(ChatMessage)
            .where("chat_id", chat_id)
            .where_op("seq", Op.GT, last_sent_seq)
            .where_op("seq", Op.LE, tick.seq)
            .build()
        )
        page = await messages_storage.find(
            pred, OffsetPage(offset=0, length=200),
            order_by=[OrderBy(field="seq", direction="asc")],
        )
        for row in page.items:
            try:
                await websocket.send_json(_message_to_wire(row))
            except WebSocketDisconnect:
                return
            last_sent_seq = row.seq


async def _send_loop_instrumented(
    websocket: WebSocket,
    chat_id: str,
    messages_storage,
    tick_sub,
    last_sent_seq: int,
    *,
    kind: str,
) -> None:
    """Wrapper around :func:`_send_loop` that increments the frames-sent counter."""
    from primer.model.storage import OffsetPage

    async for tick in tick_sub:
        if tick.seq <= last_sent_seq:
            continue
        pred = (
            Q(ChatMessage)
            .where("chat_id", chat_id)
            .where_op("seq", Op.GT, last_sent_seq)
            .where_op("seq", Op.LE, tick.seq)
            .build()
        )
        page = await messages_storage.find(
            pred, OffsetPage(offset=0, length=200),
            order_by=[OrderBy(field="seq", direction="asc")],
        )
        for row in page.items:
            try:
                await websocket.send_json(_message_to_wire(row))
                _metrics.ws_frames_sent_total.labels(kind).inc()
            except WebSocketDisconnect:
                return
            last_sent_seq = row.seq


async def _append_user_message_row(
    chat,
    messages_storage,
    chats_storage,
    parts: list,
) -> None:
    """Persist a ``user_message`` ChatMessage row and update the chat row.

    Sets ``chat.last_seq``, derives the title on the first turn, and
    persists both. The title derivation mirrors the ChatTurnRunner path
    so the chat list label is set even before the worker processes the
    message.
    """
    from primer.model.chat import TextPart

    flat_text = "\n".join(
        p.text for p in parts if isinstance(p, TextPart) and p.text
    )
    payload: dict[str, Any] = {
        "parts": [p.model_dump(mode="json") for p in parts],
    }
    if flat_text:
        payload["content"] = flat_text
    next_seq = chat.last_seq + 1
    row = ChatMessage(
        id=ChatMessage.make_id(chat.id, next_seq),
        chat_id=chat.id,
        seq=next_seq,
        kind="user_message",
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )
    await messages_storage.create(row)
    if chat.title is None:
        from primer.chat.executor import _derive_chat_title
        chat.title = _derive_chat_title(parts)
    chat.last_seq = next_seq
    await chats_storage.update(chat)


async def _maybe_auto_reject_pending_approval(
    *,
    chat,
    event_bus,
    note: str,
) -> None:
    """Auto-publish a rejection if the chat is parked on _approval."""
    if chat.parked_status not in ("parked", "resumable"):
        return
    blob = chat.parked_state or {}
    yielded = blob.get("yielded") or {}
    if yielded.get("tool_name") != "_approval":
        return
    event_key = yielded.get("event_key")
    if not event_key:
        return
    await event_bus.publish(
        event_key,
        {"decision": "rejected", "reason": note},
    )


def _parse_user_message_parts(frame: dict[str, Any]) -> list:
    """Validate an incoming user_message frame; return the list of parts.

    Accepts two shapes:

    1. ``{"content": "<text>"}`` — legacy. Converted to one TextPart.
    2. ``{"parts": [<Part>, ...]}`` — structured. Each entry must be a
       discriminated Part dict (``{"type": "text"|"image"|"document"
       |"audio"|"video", ...}``); parsing goes through Pydantic's
       Part union so the same validation the LLM layer would apply
       runs at the WS boundary.

    Frames may legally include both — the content text is folded in
    as a leading TextPart so a client can send "look at this:" with
    an image attached in one frame.

    Raises ``ValueError`` (caller turns it into an error frame) when
    the frame contains neither a non-empty content string nor a
    non-empty parts list, or any part fails schema validation.
    """
    from pydantic import TypeAdapter, ValidationError

    from primer.model.chat import Part, TextPart

    content = frame.get("content")
    raw_parts = frame.get("parts")

    if not isinstance(content, str):
        content = ""
    text = content.strip()

    if raw_parts is None and not text:
        raise ValueError(
            "user_message must include 'content' (non-empty string) "
            "or 'parts' (non-empty list)"
        )

    out: list = []
    if text:
        out.append(TextPart(text=text))

    if raw_parts is not None:
        if not isinstance(raw_parts, list) or not raw_parts:
            raise ValueError(
                "user_message.parts must be a non-empty list when present"
            )
        adapter = TypeAdapter(Part)
        for idx, entry in enumerate(raw_parts):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"user_message.parts[{idx}] must be an object, "
                    f"got {type(entry).__name__}"
                )
            kind_tag = entry.get("type")
            if kind_tag in ("tool_call", "tool_result"):
                # The Part union accepts these for assistant/tool
                # history rebuilds, but a user_message frame must
                # not carry them — they describe model behaviour,
                # not human input.
                raise ValueError(
                    f"user_message.parts[{idx}] of type {kind_tag!r} "
                    "is not allowed in a user_message frame"
                )
            try:
                out.append(adapter.validate_python(entry))
            except ValidationError as exc:
                raise ValueError(
                    f"user_message.parts[{idx}] failed validation: {exc}"
                ) from exc

    if not out:
        raise ValueError(
            "user_message must include 'content' or 'parts'"
        )
    return out


__all__ = [
    "ChatCreateBody",
    "chats_router",
    "create_chat",
    "end_chat",
    "get_chat",
    "list_chat_messages",
    "list_chats",
]
