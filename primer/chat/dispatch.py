"""Worker-side chat-turn dispatch.

One ``run_one_chat_turn`` invocation per claimed ChatLease. The
worker pool's chat claim loop creates these as background tasks;
each task drains the FIFO queue of user_messages on its claimed
chat, runs the LLM stream per message, persists rows to storage,
publishes tick events on the bus, honours interrupt requests, and
releases the lease when done (or when parking on a yielding tool).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from primer.chat.executor import ChatTurnRunner
from primer.chat.tick_router import ChatTickRouter
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
from primer.model.agent import Agent, _validate_response_format_schema
from primer.model.chat import TextPart
from primer.model.chats import Chat, ChatMessage
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.provider import LLMProvider
from primer.model.storage import (
    FieldRef, Op, OffsetPage, OrderBy, Predicate, Value,
)
from primer.model.yield_ import YieldToWorker


logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 10.0


async def _apply_switch_handoff(
    runner: ChatTurnRunner,
    chat: Chat,
    exc: YieldToWorker,
    deps: ChatDispatchDeps,
) -> str:
    """End the current turn, switch the chat's agent, append a ``handoff``
    ``agent_marker`` row (Task A5) so the timeline records the attribution
    boundary, and queue the handoff prompt as the next user_message so
    the new agent runs it. Returns the turn_status the caller should
    release with (``'claimable'`` — the queued handoff is re-served and
    runs under the new agent)."""
    from primer.chat.enqueue import append_agent_marker, append_user_message

    old_agent_id = chat.agent_id
    await runner.handle_switch(chat, exc)
    chat_storage = deps.storage_provider.get_storage(Chat)
    fresh = await chat_storage.get(chat.id)
    if fresh is not None:
        await append_agent_marker(
            fresh, deps.storage_provider,
            marker="handoff", agent_id=fresh.agent_id,
            from_agent_id=old_agent_id,
        )
        if deps.event_bus is not None:
            await deps.event_bus.publish(
                f"chat:{chat.id}:tick", {"seq": fresh.last_seq},
            )
        if fresh.pending_handoff:
            await append_user_message(
                chat=fresh,
                parts=[TextPart(text=fresh.pending_handoff)],
                storage_provider=deps.storage_provider,
            )
            fresh.pending_handoff = None
            await chat_storage.update(fresh)
    return "claimable"  # re-serve: new claim runs the new agent


@dataclass
class ChatDispatchDeps:
    """Bundle of runtime dependencies the worker injects per task."""
    storage_provider: StorageProvider
    provider_registry: Any  # ProviderRegistry — avoid import cycle
    event_bus: EventBus
    chat_tick_router: ChatTickRouter

    # Optional test seam: when set, dispatch uses this LLM regardless
    # of provider_registry resolution. Tests pin a fake LLM here.
    fake_llm: Any | None = None

    # Optional chat->channel relay. None in pure-storage tests / unbound chats.
    chat_channel_dispatcher: Any | None = None

    # Optional artifact registry for rehydrating media parts (artifact_id ->
    # inline data) before the LLM turn. None in pure-storage tests; parts then
    # pass through unhydrated (harmless for text-only chats).
    artifact_storage_registry: Any | None = None


async def _hydrate_media_parts(deps: "ChatDispatchDeps", parts: list) -> list:
    """Replace ``artifact_id`` references with inline ``data`` so the LLM turn
    sees the bytes. No-op when no artifact registry is wired or no part
    references an artifact."""
    reg = deps.artifact_storage_registry
    if reg is None or not any(getattr(p, "artifact_id", None) for p in parts):
        return parts
    try:
        store = await reg.get_default()
    except Exception:
        logger.warning("media hydration: no default artifact store; skipping")
        return parts
    from primer.channel.media import hydrate_part
    out = []
    for p in parts:
        if getattr(p, "artifact_id", None):
            try:
                out.append(await hydrate_part(store, p))
            except Exception:
                logger.warning("media hydration failed for a part; dropping it")
                continue
        else:
            out.append(p)
    return out


async def _relay_final_text(deps: "ChatDispatchDeps", chat_id: str) -> None:
    """Post the turn's final assistant text to the bound channel (relay_mode
    'final'). No-op when no dispatcher is wired or the chat is unbound.

    Text derivation lives in :func:`derive_final_relay_text` so the API-side
    relay forwarder reconstructs the exact same text from storage after an
    out-of-proc worker signals a relay over the bus."""
    if deps.chat_channel_dispatcher is None:
        return
    from primer.channel.chat_dispatcher import derive_final_relay_text
    text = await derive_final_relay_text(deps.storage_provider, chat_id)
    if text:
        await deps.chat_channel_dispatcher.relay_text(chat_id=chat_id, text=text)


async def _relay_final_media(deps: "ChatDispatchDeps", chat_id: str) -> None:
    """Relay any media parts produced by the completed turn to the bound
    channel. No-op when no dispatcher is wired or the turn produced no media."""
    if deps.chat_channel_dispatcher is None:
        return
    from primer.channel.chat_dispatcher import derive_final_relay_media
    parts = await derive_final_relay_media(deps.storage_provider, chat_id)
    if parts:
        await deps.chat_channel_dispatcher.relay_media(chat_id=chat_id, parts=parts)


async def _forward_chat_gate(deps: "ChatDispatchDeps", chat_id: str) -> None:
    """Forward a freshly-set pending gate to the bound channel.

    The envelope is built by :func:`derive_chat_gate_envelope` (shared with
    the API-side relay forwarder) from the persisted ``pending_tool_call``."""
    if deps.chat_channel_dispatcher is None:
        return
    from primer.channel.chat_dispatcher import derive_chat_gate_envelope
    env = await derive_chat_gate_envelope(deps.storage_provider, chat_id)
    if env is None:
        return
    await deps.chat_channel_dispatcher.dispatch_gate(chat_id=chat_id, envelope=env)


async def run_one_chat_turn(
    deps: ChatDispatchDeps,
    *,
    chat_id: str,
    worker_id: str,
) -> str:
    """Drain the chat's user_message queue until empty / park / cancel.

    The chat row MUST already be in ``turn_status='running'`` when this
    is called — the caller (the worker pool's claim loop) has already
    done that atomically via the ClaimEngine.

    Returns the terminal ``turn_status`` DISPOSITION (``'idle'`` or
    ``'claimable'``); this function does NOT write turn_status itself.
    The caller (the worker pool) maps the disposition to a
    ``ReleaseOutcome`` and the fenced ``ChatClaimAdapter.on_release`` is
    the single authority that persists the terminal turn_status. This
    avoids the historical double-write (unfenced here + fenced adapter)
    that could leave conflicting values on the error path.

    On clean drain / park (YieldToWorker) / cancel: ``'idle'``.
    On a turn that raised: ``'claimable'`` (so it is re-served).
    On lease loss (engine heartbeat): the pool cancels this task; the
    in-flight turn raises ``CancelledError`` and the lease release is
    fenced by the engine, so no stale writes land.
    """
    chat_storage = deps.storage_provider.get_storage(Chat)
    msg_storage = deps.storage_provider.get_storage(ChatMessage)

    chat = await chat_storage.get(chat_id)
    if chat is None:
        logger.warning("chat %s vanished before dispatch", chat_id)
        return "idle"

    cancel_event = asyncio.Event()

    runner, build_error = await _build_runner(deps, chat, cancel_event)
    if runner is None:
        await _persist_build_error(deps, chat, worker_id, build_error)
        return "idle"

    cancel_task = asyncio.create_task(
        _cancel_watcher(deps, chat_id, cancel_event),
        name=f"chat-cancel-{chat_id}",
    )

    try:
        while True:
            chat = await chat_storage.get(chat_id)
            if chat is None:
                return
            if chat.cancel_requested_at is not None:
                cancel_event.set()
            if chat.pending_tool_call is not None:
                # Cancel-while-awaiting: a cancel arrived while the chat
                # was waiting on the human's reply. Spec: abandon the
                # pending call rather than consuming the next message as
                # its answer. Persist a synthetic cancelled tool_result
                # (keeps history paired), clear the pending slot + the
                # cancel flag, and fall through to process the next
                # message as a FRESH turn.
                if chat.cancel_requested_at is not None:
                    await runner.abandon_pending(chat, chat.pending_tool_call)
                    cancel_event.clear()
                    cleared = await chat_storage.get(chat_id)
                    if cleared is not None:
                        if cleared.cancel_requested_at is not None:
                            cleared.cancel_requested_at = None
                            await chat_storage.update(cleared)
                        chat = cleared
                    continue
                # Resume path: the chat parked on a yielding tool
                # (ask_user / approval). The parked turn persisted NO
                # terminal row, so _find_next_user_message would re-serve
                # the ORIGINAL prompting message; instead locate the
                # human's reply (the first user_message after the pending
                # tool_call row). If none has arrived yet, release idle
                # and wait for the next claim.
                reply_um = await _find_resume_reply(
                    deps, chat_id, chat.pending_tool_call,
                )
                if reply_um is None:
                    return "idle"
                # Consume the reply as the pending call's tool_result,
                # then continue the agent loop from the augmented
                # history. resume_pending flags the reply
                # ``_history_excluded`` so it never replays as a fresh
                # user turn, and the continuation's terminal row
                # (done/error) closes the originally-parked user_message
                # so _find_next_user_message advances past it next drain.
                try:
                    await runner.resume_pending(
                        chat, chat.pending_tool_call, reply_um,
                    )
                    refreshed = await chat_storage.get(chat_id)
                    if refreshed is not None:
                        chat = refreshed
                    async for row in runner.continue_turn(chat):
                        await deps.event_bus.publish(
                            f"chat:{chat_id}:tick", {"seq": row.seq},
                        )
                    if cancel_event.is_set():
                        cancel_event.clear()
                        cleared = await chat_storage.get(chat_id)
                        if cleared is not None and cleared.cancel_requested_at is not None:
                            cleared.cancel_requested_at = None
                            await chat_storage.update(cleared)
                    # Resumed continuation reached a terminal row: relay the
                    # turn's final assistant text + media to the bound channel.
                    await _relay_final_text(deps, chat_id)
                    await _relay_final_media(deps, chat_id)
                except YieldToWorker as exc:
                    from primer.chat.executor import _is_switch_tool
                    if _is_switch_tool(exc):
                        return await _apply_switch_handoff(
                            runner, chat, exc, deps,
                        )
                    await runner.soft_yield(chat, exc)
                    await _forward_chat_gate(deps, chat_id)
                    return "idle"
                except Exception:
                    logger.exception(
                        "chat %s resume raised; releasing claim", chat_id,
                    )
                    return "claimable"
                continue
            next_um = await _find_next_user_message(deps, chat_id)
            if next_um is None:
                final = await chat_storage.get(chat_id)
                if final is not None and final.cancel_requested_at is not None:
                    final.cancel_requested_at = None
                    await chat_storage.update(final)
                return "idle"
            # Ephemeral (this-send-only) response_format override (A2/A3):
            # stamped on the row's payload by the send path. Server-side
            # re-validate here (defense-in-depth behind the client's live
            # validation) — an invalid schema fails the turn closed with
            # an error row instead of ever reaching the LLM stream.
            ephemeral_response_format = (next_um.payload or {}).get(
                "response_format",
            )
            if ephemeral_response_format is not None:
                try:
                    _validate_response_format_schema(ephemeral_response_format)
                except ValueError as exc:
                    await _persist_invalid_response_format_error(
                        deps, chat, str(exc),
                    )
                    continue
            try:
                turn_parts = await _hydrate_media_parts(deps, _parts_from(next_um))
                async for row in runner.run_turn(
                    chat,
                    turn_parts,
                    already_persisted_user_msg=next_um,
                    response_format=ephemeral_response_format,
                ):
                    await deps.event_bus.publish(
                        f"chat:{chat_id}:tick", {"seq": row.seq},
                    )
                # Cancellation is per-turn (mirrors ChatGPT/Claude.ai):
                # if this turn was cancelled, clear the flag + event so
                # queued user_messages are NOT auto-cancelled.
                if cancel_event.is_set():
                    cancel_event.clear()
                    refreshed = await chat_storage.get(chat_id)
                    if refreshed is not None and refreshed.cancel_requested_at is not None:
                        refreshed.cancel_requested_at = None
                        await chat_storage.update(refreshed)
                # Turn reached a terminal row: relay its final assistant
                # text + media to the bound channel (no-op when unbound).
                await _relay_final_text(deps, chat_id)
                await _relay_final_media(deps, chat_id)
            except YieldToWorker as exc:
                from primer.chat.executor import _is_switch_tool
                if _is_switch_tool(exc):
                    return await _apply_switch_handoff(runner, chat, exc, deps)
                await runner.soft_yield(chat, exc)
                await _forward_chat_gate(deps, chat_id)
                return "idle"
            except Exception:
                logger.exception(
                    "chat %s turn raised; releasing claim", chat_id,
                )
                return "claimable"
    finally:
        cancel_task.cancel()
        try:
            await cancel_task
        except (asyncio.CancelledError, Exception):
            pass


async def _build_runner(
    deps: ChatDispatchDeps,
    chat: Chat,
    cancel_event: asyncio.Event,
) -> tuple[ChatTurnRunner | None, str | None]:
    """Resolve the agent + LLM + tool stack and construct the runner.

    Returns ``(runner, None)`` on success. On failure returns
    ``(None, reason)`` where ``reason`` names the specific resolution
    that failed (missing agent, unresolvable provider, model not
    registered on the provider, or an unresolvable toolset); the caller
    persists that reason on the error row and releases the claim.
    """
    chats = deps.storage_provider.get_storage(Chat)
    agents = deps.storage_provider.get_storage(Agent)
    msgs = deps.storage_provider.get_storage(ChatMessage)
    agent = await agents.get(chat.agent_id)
    if agent is None:
        return None, (
            f"agent {chat.agent_id!r} referenced by this chat no longer exists"
        )
    try:
        llm = (
            deps.fake_llm
            if deps.fake_llm is not None
            else await deps.provider_registry.get_llm(agent.model.provider_id)
        )
    except (NotFoundError, ConfigError) as exc:
        return None, (
            f"LLM provider {agent.model.provider_id!r} could not be resolved: "
            f"{exc}"
        )
    provider_rows = deps.storage_provider.get_storage(LLMProvider)
    provider_row = await provider_rows.get(agent.model.provider_id)
    if provider_row is None:
        return None, (
            f"LLM provider {agent.model.provider_id!r} configured on agent "
            f"{agent.id!r} does not exist"
        )
    llm_model = next(
        (m for m in provider_row.models if m.name == agent.model.model_name),
        None,
    )
    if llm_model is None:
        return None, (
            f"model {agent.model.model_name!r} is not registered on provider "
            f"{agent.model.provider_id!r} (it may have been renamed or removed); "
            "update the agent's model or re-add it to the provider"
        )
    toolset_ids: set[str] = set()
    for sid in (agent.tools or []):
        if "__" in sid:
            toolset_ids.add(sid.rsplit("__", 1)[0])
    toolset_providers: dict[str, Any] = {}
    for tid in toolset_ids:
        try:
            toolset_providers[tid] = await deps.provider_registry.get_toolset(tid)
        except (NotFoundError, ConfigError) as exc:
            return None, (
                f"toolset {tid!r} required by this agent could not be "
                f"resolved: {exc}"
            )
    from primer.agent.approval import ApprovalResolver
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.model.tool_approval import ToolApprovalPolicy
    approval_resolver = ApprovalResolver(
        storage=deps.storage_provider.get_storage(ToolApprovalPolicy),
    )
    tool_manager = ToolExecutionManager(
        toolset_providers=toolset_providers,
        provider_registry=deps.provider_registry,
        tools=agent.tools,
        approval_resolver=approval_resolver,
        chat_id=chat.id,
    )
    # No inform sink is wired on the chat surface yet: inform_user in a chat
    # returns delivered_to:0 for now. Chat-side inform delivery is deferred to
    # the channels-drive-chats sub-project, which must persist the inform line
    # without splitting a multi-tool batch's tool_result rows.
    # Resolve the default artifact store (if wired) so tool-produced media
    # (MCP image/audio results) is captured into tool_result rows and can be
    # relayed to the bound channel.
    artifact_store = None
    if deps.artifact_storage_registry is not None:
        try:
            artifact_store = await deps.artifact_storage_registry.get_default()
        except Exception:
            logger.warning("chat runner: no default artifact store available")
    from primer.model.tool_approval import ToolApprovalRecord
    # Resolution precedence (A2, chat-refactor plan §6): per-chat
    # ``Chat.response_format`` (A1) overrides the agent's default for
    # THIS chat only; falls back to the agent default when the chat has
    # no override. The ephemeral (this-send-only) layer is resolved
    # per-turn from the user_message row in ``run_one_chat_turn`` below
    # and passed into ``run_turn`` as the highest-precedence override.
    effective_response_format = (
        chat.response_format
        if chat.response_format is not None
        else agent.response_format
    )
    return ChatTurnRunner(
        agent=agent,
        llm=llm,
        llm_model=llm_model,
        tool_manager=tool_manager,
        chat_storage=chats,
        message_storage=msgs,
        cancel_event=cancel_event,
        artifact_storage=artifact_store,
        approval_record_storage=deps.storage_provider.get_storage(
            ToolApprovalRecord
        ),
        response_format=effective_response_format,
    ), None


async def _persist_build_error(
    deps: ChatDispatchDeps,
    chat: Chat,
    worker_id: str,
    reason: str | None = None,
) -> None:
    chats = deps.storage_provider.get_storage(Chat)
    msgs = deps.storage_provider.get_storage(ChatMessage)
    next_seq = chat.last_seq + 1
    message = (
        f"could not build chat runner: {reason}"
        if reason
        else "could not build chat runner"
    )
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id(chat.id, next_seq),
        chat_id=chat.id, seq=next_seq, kind="error",
        payload={"message": message,
                 "code": "runner_build_failed"},
        created_at=datetime.now(timezone.utc),
    ))
    chat.last_seq = next_seq
    await chats.update(chat)
    # The terminal turn_status ('idle') is applied by the fenced adapter
    # from the 'idle' disposition run_one_chat_turn returns after this.
    await deps.event_bus.publish(
        f"chat:{chat.id}:tick", {"seq": next_seq},
    )


async def _persist_invalid_response_format_error(
    deps: ChatDispatchDeps,
    chat: Chat,
    detail: str,
) -> None:
    """Persist a terminal ``error`` row for a turn whose ephemeral
    ``response_format`` failed server-side re-validation (A2), so the
    turn is skipped closed rather than ever reaching the LLM stream.

    This closes the turn (an ``error`` row is a member of ``_TERMINALS``)
    so :func:`_find_next_user_message` advances past the offending
    user_message on the next drain instead of re-serving it forever.
    """
    chats = deps.storage_provider.get_storage(Chat)
    msgs = deps.storage_provider.get_storage(ChatMessage)
    next_seq = chat.last_seq + 1
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id(chat.id, next_seq),
        chat_id=chat.id, seq=next_seq, kind="error",
        payload={
            "message": f"invalid response_format: {detail}",
            "code": "invalid_response_format",
        },
        created_at=datetime.now(timezone.utc),
    ))
    chat.last_seq = next_seq
    await chats.update(chat)
    await deps.event_bus.publish(
        f"chat:{chat.id}:tick", {"seq": next_seq},
    )


async def _cancel_watcher(
    deps: ChatDispatchDeps,
    chat_id: str,
    cancel_event: asyncio.Event,
) -> None:
    sub = deps.event_bus.subscribe()
    try:
        async for event in sub:
            if event.event_key == f"chat:{chat_id}:cancel":
                cancel_event.set()
                return
    except asyncio.CancelledError:
        return
    finally:
        await sub.aclose()


async def _find_next_user_message(
    deps: ChatDispatchDeps,
    chat_id: str,
) -> ChatMessage | None:
    """Find the next user_message that has not yet been processed.

    A user_message is considered processed when a terminal row
    (done / error / cancelled / yielded) exists that "follows" it in
    the turn-pairing sense. We use a count-based algorithm: count the
    number of terminal rows (each terminal closes exactly one turn) and
    return the (count + 1)th user_message in ascending seq order. When
    there are no more user_messages to process, returns None.

    This correctly handles the worker-dispatch model where user_messages
    are pre-seeded to storage before the worker claims the chat.

    Cursor optimization (perf, behaviour-equivalent)
    ------------------------------------------------
    ``Chat.next_unprocessed_seq`` records a seq below which the chat is
    KNOWN to be fully drained: every row with ``seq < cursor`` belongs to
    a completed turn (each user_message paired with its terminal). The
    scan therefore only needs to consider rows with ``seq >= cursor``,
    counting terminals/user_messages over that suffix alone. This is
    EXACTLY equivalent to the full scan: at the cursor checkpoint the
    prefix holds ``k`` non-excluded user_messages and ``k`` terminals, so
    the global ``user_messages[total_terminals]`` index reduces to
    ``suffix_user_messages[suffix_terminals]`` (the ``k`` prefix terms
    cancel). The cursor is only ever advanced to ``last_seq + 1`` at a
    fully-drained checkpoint (see below), so the invariant holds. A fresh
    chat (cursor 0) scans from the start, identical to the pre-cursor code.
    """
    msgs = deps.storage_provider.get_storage(ChatMessage)
    chats = deps.storage_provider.get_storage(Chat)
    chat = await chats.get(chat_id)
    cursor = chat.next_unprocessed_seq if chat is not None else 0

    rows = await _read_messages_from_cursor(msgs, chat_id, cursor)

    _TERMINALS = frozenset({"done", "error", "cancelled", "yielded"})
    terminal_count = 0
    user_messages: list[ChatMessage] = []
    for row in rows:
        if row.kind in _TERMINALS:
            terminal_count += 1
        elif row.kind == "user_message":
            # A reply consumed by the resume path is flagged
            # ``_history_excluded``; it has already been folded into
            # a tool_result and must NOT be re-served as a fresh turn.
            if (row.payload or {}).get("_history_excluded"):
                continue
            user_messages.append(row)

    # The Nth terminal closes the Nth user_message; the (N+1)th
    # user_message is the next one to process. If there are fewer
    # user_messages than (terminal_count + 1), the queue is drained.
    target_index = terminal_count  # 0-based index into user_messages list
    if target_index < len(user_messages):
        return user_messages[target_index]
    # Fully drained: every user_message in the suffix is paired with a
    # terminal. Advance the cursor past the rows we actually scanned so the
    # next drain skips them. We use the max seq SEEN IN THIS SNAPSHOT (never
    # a re-read last_seq) so a user_message appended concurrently -- after
    # the snapshot but before the cursor write -- is never skipped: its seq
    # is strictly greater than every row we observed, hence >= the new
    # cursor, so the next scan still finds it.
    max_seen = rows[-1].seq if rows else (cursor - 1)
    await _advance_drain_cursor(chats, chat_id, max_seen + 1, cursor)
    return None


async def _read_messages_from_cursor(
    msgs,
    chat_id: str,
    cursor: int,
) -> list[ChatMessage]:
    """Read every ChatMessage with ``seq >= cursor`` in ascending seq order.

    Pages internally (window of 200) so memory stays bounded. Filtering
    by ``seq >= cursor`` is applied in-process to keep the storage query
    backend-agnostic; the cursor still bounds how far back the page walk
    can start mattering, and skipped pages are cheap reads. When the
    cursor is 0 this returns every row, identical to the old full scan.
    """
    pred = Predicate(
        left=FieldRef(name="chat_id"), op=Op.EQ,
        right=Value(value=chat_id),
    )
    out: list[ChatMessage] = []
    offset = 0
    PAGE = 200
    while True:
        page = await msgs.find(
            pred, OffsetPage(offset=offset, length=PAGE),
            order_by=[OrderBy(field="seq", direction="asc")],
        )
        for row in page.items:
            if row.seq >= cursor:
                out.append(row)
        if len(page.items) < PAGE:
            break
        offset += PAGE
    return out


async def _advance_drain_cursor(
    chats, chat_id: str, new_cursor: int, prev_cursor: int,
) -> None:
    """Advance ``Chat.next_unprocessed_seq`` to ``new_cursor`` on a fully
    drained chat. No-op when the chat vanished or the cursor wouldn't move.

    ``new_cursor`` is ``max_scanned_seq + 1`` (NOT a re-read ``last_seq``)
    so a concurrently-appended user_message is never skipped. Only writes
    when the cursor actually advances (avoids a redundant storage
    round-trip on every idle drain)."""
    fresh = await chats.get(chat_id)
    if fresh is None:
        return
    if new_cursor <= fresh.next_unprocessed_seq or new_cursor <= prev_cursor:
        return
    fresh.next_unprocessed_seq = new_cursor
    await chats.update(fresh)


async def _find_resume_reply(
    deps: ChatDispatchDeps,
    chat_id: str,
    pending: dict,
) -> ChatMessage | None:
    """Find the human reply that resolves a pending (parked) tool call.

    The parked turn persisted its yielding ``tool_call`` row but NO
    terminal row, so the ordinary :func:`_find_next_user_message`
    cursor still points at the original prompting message. The reply is
    the first un-consumed ``user_message`` whose seq is greater than the
    pending ``tool_call`` row's seq. Returns None when no reply has
    arrived yet (the chat should release idle and wait).

    Cursor-bounded scan (perf, behaviour-equivalent): the parked turn is
    by definition NOT drained, so ``Chat.next_unprocessed_seq`` has not
    advanced past its prompting user_message; the pending ``tool_call``
    and the awaited reply both sit at ``seq >= cursor``. Scanning from the
    cursor therefore returns exactly the same reply the full scan would.
    """
    msgs = deps.storage_provider.get_storage(ChatMessage)
    chats = deps.storage_provider.get_storage(Chat)
    chat = await chats.get(chat_id)
    cursor = chat.next_unprocessed_seq if chat is not None else 0
    tool_call_id = pending.get("tool_call_id")
    rows = await _read_messages_from_cursor(msgs, chat_id, cursor)

    pending_seq: int | None = None
    for row in rows:
        if row.kind == "tool_call" and (row.payload or {}).get("id") == tool_call_id:
            pending_seq = row.seq
            break
    if pending_seq is None:
        return None
    for row in rows:
        if row.seq <= pending_seq or row.kind != "user_message":
            continue
        if (row.payload or {}).get("_history_excluded"):
            continue
        return row
    return None


async def sweep_chats(
    *,
    storage_provider: StorageProvider,
    scheduler: Any,
    event_bus: EventBus,
    heartbeat_stale_after: timedelta = timedelta(seconds=90),
) -> int:
    """Legacy sweeper — now a no-op.

    Lease-based heartbeating is handled by the ClaimEngine; the pool's
    heartbeat loop detects lost leases and signals the dispatch task
    directly. This function is retained for API compatibility but no
    longer inspects or mutates chat rows.
    """
    return 0


def _parts_from(user_message_row: ChatMessage) -> list:
    """Convert the persisted user_message payload back into Parts."""
    payload = user_message_row.payload or {}
    raw_parts = payload.get("parts")
    if isinstance(raw_parts, list) and raw_parts:
        from pydantic import TypeAdapter
        from primer.model.chat import Part
        adapter = TypeAdapter(Part)
        out = []
        for entry in raw_parts:
            try:
                out.append(adapter.validate_python(entry))
            except Exception:
                continue
        if out:
            return out
    text = payload.get("content") or ""
    return [TextPart(text=text)]


__all__ = ["ChatDispatchDeps", "run_one_chat_turn", "sweep_chats"]
