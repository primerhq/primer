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
:mod:`matrix.chat.executor`) for the M6 scaffold; the agent loop
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

from matrix.api.deps import get_agent_storage, get_storage_provider
from matrix.api.errors import common_responses
from matrix.api.pagination import parse_page
from matrix.chat.executor import ChatTurnRunner
from matrix.model.agent import Agent
from matrix.model.chats import Chat, ChatMessage
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.storage import (
    FieldRef,
    Op,
    OrderBy,
    PageRequest,
    Predicate,
    Value,
)


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
            Predicate(
                left=FieldRef(name="agent_id"),
                op=Op.EQ,
                right=Value(value=agent_id),
            ),
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
    response_model=Chat,
    summary="End a chat — transitions to status='ended'",
    responses=common_responses(404, 409, 500),
)
async def end_chat(
    chat_id: str = Path(...),
    sp=Depends(get_storage_provider),
) -> Chat:
    storage = sp.get_storage(Chat)
    chat = await storage.get(chat_id)
    if chat is None:
        raise NotFoundError(f"Chat {chat_id!r} does not exist")
    if chat.status == "ended":
        raise ConflictError(f"Chat {chat_id!r} is already ended")
    chat.status = "ended"
    return await storage.update(chat)


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
    predicates: list[Predicate] = [
        Predicate(
            left=FieldRef(name="chat_id"),
            op=Op.EQ,
            right=Value(value=chat_id),
        ),
    ]
    if after_seq is not None:
        predicates.append(
            Predicate(
                left=FieldRef(name="seq"),
                op=Op.GT,
                right=Value(value=after_seq),
            )
        )
    pred = predicates[0]
    for p in predicates[1:]:
        pred = Predicate(left=pred, op=Op.AND, right=p)
    return await messages.find(
        pred, page, order_by=[OrderBy(field="seq", direction="asc")],
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
    from matrix.model.storage import OffsetPage

    messages_storage = sp.get_storage(ChatMessage)
    cur = cursor
    PAGE = 200
    last_emitted = cur
    while True:
        pred = Predicate(
            left=Predicate(
                left=FieldRef(name="chat_id"),
                op=Op.EQ,
                right=Value(value=chat_id),
            ),
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="seq"),
                op=Op.GT,
                right=Value(value=cur),
            ),
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
    """Bidirectional chat stream.

    Lifecycle:

    1. Accept the upgrade.
    2. Resolve the chat row. Reject (close 4404) if missing.
    3. Replay any chat_messages with ``seq > cursor`` in order.
    4. Loop:
       - read client message (user_message / interrupt / ping)
       - run the chat turn for user_message
       - stream resulting rows back
    """
    # FastAPI's dependency system doesn't reach websocket routes the
    # same way; pull the storage provider straight off app.state.
    sp = websocket.app.state.storage_provider
    chats_storage = sp.get_storage(Chat)
    messages_storage = sp.get_storage(ChatMessage)
    event_bus = getattr(websocket.app.state, "event_bus", None)

    chat = await chats_storage.get(chat_id)
    if chat is None:
        # 4404 = application-defined "not found" close code per RFC 6455
        # §7.4 (the 4000-4999 range is reserved for app use). MUST
        # accept() first — close() before accept() makes Starlette
        # reject the handshake with HTTP 403, which clients see as a
        # generic handshake failure, not the documented close code.
        await websocket.accept()
        await websocket.close(code=4404, reason=f"chat {chat_id!r} not found")
        return
    if chat.status == "ended":
        await websocket.accept()
        await websocket.close(code=4410, reason="chat ended")
        return

    await websocket.accept()
    try:
        last_seq = await _replay_since_cursor(
            websocket, chat_id, cursor, sp,
        )

        runner = ChatTurnRunner(
            chat_storage=chats_storage,
            message_storage=messages_storage,
        )

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
                if event_bus is not None:
                    await _maybe_auto_reject_pending_approval(
                        chat=chat,
                        event_bus=event_bus,
                        note="interrupted by operator",
                    )
                # M6 stub: emit an error marker. Real implementation
                # would signal the in-flight turn to stop.
                await _append_and_send(
                    websocket,
                    chat,
                    chats_storage,
                    messages_storage,
                    kind="error",
                    payload={"message": "interrupted by client"},
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
                if event_bus is None:
                    await websocket.send_json({
                        "kind": "error",
                        "code": "tool_approval_no_event_bus",
                        "message": "event bus not available",
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
            content = incoming.get("content", "")
            if not isinstance(content, str) or not content.strip():
                await websocket.send_json(
                    {
                        "kind": "error",
                        "message": "user_message.content must be a non-empty string",
                    }
                )
                continue
            if event_bus is not None:
                await _maybe_auto_reject_pending_approval(
                    chat=chat,
                    event_bus=event_bus,
                    note="superseded by new user input",
                )
            # Re-fetch the chat row so the runner sees the latest
            # last_seq (another worker may have appended messages).
            chat = await chats_storage.get(chat_id)
            if chat is None or chat.status == "ended":
                await websocket.close(
                    code=4410, reason="chat ended mid-stream",
                )
                return
            try:
                async for row in runner.run_turn(chat, content):
                    await websocket.send_json(_message_to_wire(row))
                    last_seq = row.seq
            except Exception as exc:  # noqa: BLE001
                logger.exception("chat %s turn failed: %s", chat_id, exc)
                # Persist an error row + propagate.
                await _append_and_send(
                    websocket,
                    chat,
                    chats_storage,
                    messages_storage,
                    kind="error",
                    payload={"message": str(exc)},
                )
    except WebSocketDisconnect:
        return


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


async def _append_and_send(
    websocket: WebSocket,
    chat: Chat,
    chats_storage,
    messages_storage,
    *,
    kind: str,
    payload: dict[str, Any],
) -> None:
    """Helper: persist a row + flush it to the WS in one call."""
    next_seq = chat.last_seq + 1
    row = ChatMessage(
        id=ChatMessage.make_id(chat.id, next_seq),
        chat_id=chat.id,
        seq=next_seq,
        kind=kind,  # type: ignore[arg-type]
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )
    await messages_storage.create(row)
    chat.last_seq = next_seq
    await chats_storage.update(chat)
    await websocket.send_json(_message_to_wire(row))


__all__ = [
    "ChatCreateBody",
    "chats_router",
    "create_chat",
    "end_chat",
    "get_chat",
    "list_chat_messages",
    "list_chats",
]
