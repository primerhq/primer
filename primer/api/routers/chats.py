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
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_agent_storage,
    get_claim_engine,
    get_provider_registry,
    get_storage_provider,
)
from primer.api.errors import common_responses
from primer.api.pagination import parse_page
from primer.model.agent import Agent
from primer.model.chats import Chat, ChatMessage
from primer.model.except_ import ConfigError, ConflictError, NotFoundError
from primer.model.provider import LLMProvider
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
        # Drop the per-chat usage cache entry so subsequent recreations
        # of a same-named chat start clean.
        from primer.chat.usage_cache import clear_usage
        clear_usage(chat_id)
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
    before_seq: Annotated[
        int | None,
        Query(
            ge=0,
            description=(
                "Return only messages with seq < this value, ordered "
                "DESC + limited then reversed so the response is the "
                "most recent N below the cursor (still ASC). Pass a "
                "very large value to fetch the tail of the chat; pass "
                "the oldest-loaded seq to lazy-load older history."
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
    if before_seq is not None:
        q = q.where_op("seq", Op.LT, before_seq)
    if before_seq is not None:
        # Tail mode: take the N highest seqs under the cursor, then
        # reverse for ASC output so the renderer can append directly.
        result = await messages.find(
            q.build(), page,
            order_by=[OrderBy(field="seq", direction="desc")],
        )
        result.items.reverse()
        return result
    return await messages.find(
        q.build(), page, order_by=[OrderBy(field="seq", direction="asc")],
    )


# ===========================================================================
# REST: on-demand compaction
# ===========================================================================


class CompactResponse(BaseModel):
    """Body of a successful ``POST /v1/chats/{id}/compact``."""

    compaction_marker_seq: int = Field(
        ...,
        description=(
            "Sequence number of the persisted ``compaction_marker`` row. "
            "Clients can use this with the cursor-replay surface to "
            "fetch the marker payload directly."
        ),
    )
    summary: str = Field(
        ...,
        description="Summary text the LLM produced for the rolled-up history.",
    )
    tokens_before: int = Field(
        ..., description="Estimated token count before compaction."
    )
    tokens_after: int = Field(
        ..., description="Estimated token count after compaction."
    )


@chats_router.post(
    "/chats/{chat_id}/compact",
    response_model=CompactResponse,
    summary="Force-compact a chat's history on demand",
    responses=common_responses(404, 409, 422, 500, 503),
)
async def compact_chat(
    request: Request,
    chat_id: str = Path(...),
    sp=Depends(get_storage_provider),
    agents=Depends(get_agent_storage),
    provider_registry=Depends(get_provider_registry),
) -> CompactResponse:
    """Run :func:`primer.agent.compaction_mixin.force_compact` against
    the chat's current history, persist a ``compaction_marker`` row,
    and return the summary.

    Status codes:

    * ``200`` — compaction ran; body carries ``compaction_marker_seq``,
      ``summary``, ``tokens_before``, ``tokens_after``.
    * ``404`` — no such chat.
    * ``409`` — a worker turn is in flight (``turn_status='running'``).
      Forcing compaction would race the runner's own pre-turn auto-
      compact pass and risk a double-marker.
    * ``503`` — the chat's agent has no resolvable LLM provider /
      model. Compaction needs the LLM to produce the summary; the
      surface raises :class:`ConfigError` (rendered as 503) so the
      operator can fix the agent's provider binding.

    Implementation mirrors :func:`primer.chat.dispatch._build_runner`'s
    resolution path (agent → provider → model row) so the same
    failures land in the same buckets.
    """
    from primer.agent.compaction import CompactionStrategy
    from primer.agent.compaction_mixin import force_compact
    from primer.agent.prompts import DEFAULT_COMPACTION_PROMPT
    from primer.chat.executor import ChatTurnRunner

    # 1) Chat exists?
    chat_storage = sp.get_storage(Chat)
    chat = await chat_storage.get(chat_id)
    if chat is None:
        raise NotFoundError(f"Chat {chat_id!r} does not exist")

    # 2) No in-flight turn?
    if chat.turn_status == "running":
        raise ConflictError(
            f"Chat {chat_id!r} has a turn in flight; "
            "wait for it to finish before compacting."
        )

    # 3) Resolve agent + LLM + model row.
    agent = await agents.get(chat.agent_id)
    if agent is None:
        raise ConfigError(
            f"Chat {chat_id!r} references agent {chat.agent_id!r} which "
            "no longer exists; cannot compact."
        )
    try:
        llm = await provider_registry.get_llm(agent.model.provider_id)
    except (NotFoundError, ConfigError) as exc:
        raise ConfigError(
            f"Agent {agent.id!r} has no resolvable LLM provider "
            f"({agent.model.provider_id!r}): {exc}"
        ) from exc
    provider_rows = sp.get_storage(LLMProvider)
    provider_row = await provider_rows.get(agent.model.provider_id)
    if provider_row is None:
        raise ConfigError(
            f"LLMProvider {agent.model.provider_id!r} configured on "
            f"agent {agent.id!r} does not exist."
        )
    llm_model = next(
        (m for m in provider_row.models if m.name == agent.model.model_name),
        None,
    )
    if llm_model is None:
        raise ConfigError(
            f"Model {agent.model.model_name!r} is not enabled on "
            f"provider {agent.model.provider_id!r}."
        )

    # 4) Load current history via the runner's helper so compaction-
    #    marker reassembly stays consistent with pre-turn compaction.
    messages_storage = sp.get_storage(ChatMessage)
    runner = ChatTurnRunner.__new__(ChatTurnRunner)
    runner._agent = agent
    runner._llm = llm
    runner._model = llm_model
    runner._tools = None  # _load_history doesn't touch tools
    runner._chats = chat_storage
    runner._messages = messages_storage
    runner._cancel_event = None
    runner._marker_persisted = False
    runner._last_input_tokens = None
    runner._last_output_tokens = None
    history = await runner._load_history(chat_id)

    # 5) Force-compact (bypasses the should_compact threshold check).
    compaction_prompt_field = getattr(agent, "compaction_prompt", None)
    if compaction_prompt_field:
        compaction_prompt = "\n\n".join(compaction_prompt_field)
        prompt_source = "custom"
    else:
        compaction_prompt = DEFAULT_COMPACTION_PROMPT
        prompt_source = "default"
    result = await force_compact(
        llm=llm,
        strategy=CompactionStrategy(),
        history=list(history),
        compaction_prompt=compaction_prompt,
        model_name=llm_model.name,
        context_length=llm_model.context_length,
    )

    # 6) Persist the compaction_marker row + bump chat.last_seq.
    #    Refresh the chat row first to absorb any concurrent last_seq
    #    bump (the dispatcher path goes through the same fields).
    fresh = await chat_storage.get(chat_id)
    if fresh is None:  # racing delete
        raise NotFoundError(f"Chat {chat_id!r} does not exist")
    next_seq = fresh.last_seq + 1
    marker = ChatMessage(
        id=ChatMessage.make_id(chat_id, next_seq),
        chat_id=chat_id,
        seq=next_seq,
        kind="compaction_marker",
        payload={
            "summary": result.summary_text,
            "replaced_from_seq": 1,
            "replaced_to_seq": next_seq - 1,
            "model": llm_model.name,
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "compaction_prompt_source": prompt_source,
            "created_at": result.created_at.isoformat(),
            "trigger": "operator_forced",
        },
        created_at=result.created_at,
    )
    await messages_storage.create(marker)
    fresh.last_seq = next_seq
    await chat_storage.update(fresh)

    # 7) Publish a per-chat tick so any live WS subscriber picks up
    #    the new marker row + the translated ``compaction`` envelope
    #    (spec §6.4). Mirrors the dispatcher's per-row tick in
    #    ``primer.chat.dispatch``. The bus is optional — when absent
    #    the row is still persisted and the next cursor-replay covers
    #    the gap.
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is not None:
        try:
            await event_bus.publish(
                f"chat:{chat_id}:tick", {"seq": next_seq},
            )
        except Exception:  # noqa: BLE001 — never break the REST response
            logger.exception(
                "compact_chat: failed to publish tick for chat %s", chat_id,
            )

    return CompactResponse(
        compaction_marker_seq=next_seq,
        summary=result.summary_text,
        tokens_before=result.tokens_before,
        tokens_after=result.tokens_after,
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


def _compaction_envelope(msg: ChatMessage) -> dict[str, Any]:
    """Translate a ``compaction_marker`` row into the WS 'compaction' envelope.

    Spec §6.4: the on-the-wire ``compaction`` envelope carries the
    rolled-up summary plus before/after token counts and the seq
    range that the marker replaced. Mirrors the payload shape
    persisted in :func:`compact_chat` / :class:`ChatTurnRunner._maybe_compact_history`.
    """
    payload = msg.payload or {}
    return {
        "kind": "compaction",
        "seq": msg.seq,
        "summary": payload.get("summary", ""),
        "tokens_before": payload.get("tokens_before", 0),
        "tokens_after": payload.get("tokens_after", 0),
        "replaced_from_seq": payload.get("replaced_from_seq"),
        "replaced_to_seq": payload.get("replaced_to_seq"),
    }


def _usage_envelope(chat_id: str, context_length: int) -> dict[str, Any]:
    """Build the WS 'usage' envelope for a chat using the cached counters.

    Spec §6.4: every WS session starts with an initial ``usage``
    frame (zeros if nothing has run yet) and re-emits one after each
    ``done`` row so the context-meter UI stays in sync.
    """
    from primer.chat.usage_cache import get_usage
    cached = get_usage(chat_id)
    used_pct = (
        cached["input_tokens"] / context_length
        if context_length > 0 else 0.0
    )
    return {
        "kind": "usage",
        "seq": None,
        "input_tokens": cached["input_tokens"],
        "output_tokens": cached["output_tokens"],
        "context_length": context_length,
        "used_pct": used_pct,
    }


async def _resolve_context_length(sp, chat_id: str) -> int:
    """Resolve the agent's model ``context_length`` for a chat.

    Walks ``chat → agent → llm_provider → model`` and returns the
    model's ``context_length``. Returns 0 on any missing link — the
    ``usage`` envelope just degrades to ``used_pct=0.0`` rather than
    failing the WS upgrade. Cached once per WS session in
    :func:`chat_ws` so we don't re-resolve every turn.
    """
    chat_storage = sp.get_storage(Chat)
    chat = await chat_storage.get(chat_id)
    if chat is None or not chat.agent_id:
        return 0
    agent = await sp.get_storage(Agent).get(chat.agent_id)
    if agent is None or agent.model is None:
        return 0
    provider_id = agent.model.provider_id
    model_name = agent.model.model_name
    if not provider_id or not model_name:
        return 0
    provider_row = await sp.get_storage(LLMProvider).get(provider_id)
    if provider_row is None:
        return 0
    for m in provider_row.models:
        if m.name == model_name:
            return m.context_length
    return 0


def _message_to_wire(msg: ChatMessage) -> dict[str, Any]:
    """Render a :class:`ChatMessage` for the WebSocket protocol.

    The wire envelope merges the kind-specific payload into the
    top-level object alongside ``seq`` and ``kind`` — that matches
    the spec's TypeScript union (``{ kind: ..., seq: ..., delta:
    ..., ... }``) and means clients don't need to unwrap a nested
    ``payload`` key.

    ``compaction_marker`` rows are translated to the dedicated
    ``compaction`` envelope (spec §6.4) so client code can branch on
    a stable envelope kind regardless of the underlying row schema.
    """
    if msg.kind == "compaction_marker":
        return _compaction_envelope(msg)
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

            # Resolve context_length once for this session; cheap, but
            # the lookup walks three storage rows. The cached usage
            # counters live in primer.chat.usage_cache and are updated
            # by ChatTurnRunner after every Usage event.
            context_length = await _resolve_context_length(sp, chat_id)

            # Spec §6.4: send an initial ``usage`` frame so the
            # client's TokenMeter renders immediately on connect,
            # even if no turn has run yet (counters → zeros).
            try:
                await websocket.send_json(
                    _usage_envelope(chat_id, context_length),
                )
            except WebSocketDisconnect:
                return

            tick_sub = chat_tick_router.subscribe(chat_id)
            try:
                recv_task = asyncio.ensure_future(
                    _recv_loop(
                        websocket, chat_id, chats_storage, messages_storage, event_bus,
                        claim_engine=claim_engine,
                        storage_provider=sp,
                    )
                )
                send_task = asyncio.ensure_future(
                    _send_loop_instrumented(
                        websocket, chat_id, messages_storage, tick_sub, last_seq,
                        kind="chat",
                        context_length=context_length,
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
    storage_provider=None,
) -> None:
    """Read client frames and dispatch them.

    - ``ping`` → immediate pong.
    - ``interrupt`` → set cancel_requested_at + publish cancel event.
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
        # Persist the user_message row + update chat.last_seq / title.
        # Delegate to the canonical service helper so the WS path and
        # the trigger dispatcher write user_messages identically.
        from primer.chat.enqueue import append_user_message

        await append_user_message(
            chat=chat,
            parts=user_parts,
            storage_provider=storage_provider,
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
    context_length: int = 0,
) -> None:
    """Wrapper around :func:`_send_loop` that increments the frames-sent counter.

    Also re-emits a ``usage`` envelope after every ``done`` row so the
    UI's TokenMeter stays in sync with the cached counters that
    :class:`ChatTurnRunner` updates from Usage events.
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
                _metrics.ws_frames_sent_total.labels(kind).inc()
                # Re-emit the usage envelope after a turn closes (done) AND
                # after each tool result lands, so the context meter tracks
                # the conversation growing through a multi-tool turn rather
                # than only updating once at the very end. The cached
                # counters are refreshed per LLM call by the executor's
                # _record_usage; this just surfaces them more often.
                if row.kind in ("done", "tool_result"):
                    await websocket.send_json(
                        _usage_envelope(chat_id, context_length),
                    )
                    _metrics.ws_frames_sent_total.labels(kind).inc()
            except WebSocketDisconnect:
                return
            last_sent_seq = row.seq


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
    "CompactResponse",
    "chats_router",
    "compact_chat",
    "create_chat",
    "end_chat",
    "get_chat",
    "list_chat_messages",
    "list_chats",
]
