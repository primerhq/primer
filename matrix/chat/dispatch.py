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
from datetime import datetime, timezone
from typing import Any

from matrix.chat.executor import ChatTurnRunner
from matrix.chat.tick_router import ChatTickRouter
from matrix.int.event_bus import EventBus
from matrix.int.storage_provider import StorageProvider
from matrix.model.agent import Agent
from matrix.model.chat import TextPart
from matrix.model.chats import Chat, ChatMessage
from matrix.model.except_ import ConfigError, NotFoundError
from matrix.model.provider import LLMProvider
from matrix.model.storage import (
    FieldRef, Op, OffsetPage, OrderBy, Predicate, Value,
)
from matrix.model.yield_ import YieldToWorker
from matrix.int.scheduler import Scheduler


logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 10.0


@dataclass
class ChatDispatchDeps:
    """Bundle of runtime dependencies the worker injects per task."""
    storage_provider: StorageProvider
    provider_registry: Any  # ProviderRegistry — avoid import cycle
    scheduler: Scheduler
    event_bus: EventBus
    chat_tick_router: ChatTickRouter

    # Optional test seam: when set, dispatch uses this LLM regardless
    # of provider_registry resolution. Tests pin a fake LLM here.
    fake_llm: Any | None = None


async def run_one_chat_turn(
    deps: ChatDispatchDeps,
    *,
    chat_id: str,
    worker_id: str,
) -> None:
    """Drain the chat's user_message queue until empty / park / cancel.

    The chat row MUST already be in ``turn_status='running'`` with
    ``claimed_by=worker_id`` when this is called — the caller (the
    worker pool's claim loop) has already done that atomically.

    On clean drain: leaves the chat ``turn_status='idle'``.
    On park (YieldToWorker): releases the lease; ``turn_status``
    stays in place (the claim predicate gates on ``parked_status``).
    On cancel: persists a ``cancelled`` row, releases to idle.
    On lease loss (sweeper reclaim): exits without further writes.
    """
    chat_storage = deps.storage_provider.get_storage(Chat)
    msg_storage = deps.storage_provider.get_storage(ChatMessage)

    chat = await chat_storage.get(chat_id)
    if chat is None:
        logger.warning("chat %s vanished before dispatch", chat_id)
        return

    cancel_event = asyncio.Event()
    lease_lost = asyncio.Event()

    runner = await _build_runner(deps, chat, cancel_event)
    if runner is None:
        await _persist_build_error(deps, chat, worker_id)
        return

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(deps, chat_id, worker_id, lease_lost),
        name=f"chat-hb-{chat_id}",
    )
    cancel_task = asyncio.create_task(
        _cancel_watcher(deps, chat_id, cancel_event),
        name=f"chat-cancel-{chat_id}",
    )

    try:
        while True:
            if lease_lost.is_set():
                return
            chat = await chat_storage.get(chat_id)
            if chat is None or chat.claimed_by != worker_id:
                return
            if chat.cancel_requested_at is not None:
                cancel_event.set()
            next_um = await _find_next_user_message(deps, chat_id)
            if next_um is None:
                await deps.scheduler.release_chat(
                    chat_id, worker_id, next_turn_status="idle",
                )
                final = await chat_storage.get(chat_id)
                if final is not None and final.cancel_requested_at is not None:
                    final.cancel_requested_at = None
                    await chat_storage.update(final)
                return
            try:
                async for row in runner.run_turn(
                    chat,
                    _parts_from(next_um),
                    already_persisted_user_msg=next_um,
                ):
                    await deps.event_bus.publish(
                        f"chat:{chat_id}:tick", {"seq": row.seq},
                    )
                    if lease_lost.is_set():
                        return
            except YieldToWorker:
                await deps.scheduler.release_chat(
                    chat_id, worker_id, next_turn_status="claimable",
                )
                return
            except Exception:
                logger.exception(
                    "chat %s turn raised; releasing claim", chat_id,
                )
                await deps.scheduler.release_chat(
                    chat_id, worker_id, next_turn_status="claimable",
                )
                return
    finally:
        heartbeat_task.cancel()
        cancel_task.cancel()
        for t in (heartbeat_task, cancel_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


async def _build_runner(
    deps: ChatDispatchDeps,
    chat: Chat,
    cancel_event: asyncio.Event,
) -> ChatTurnRunner | None:
    """Resolve the agent + LLM + tool stack and construct the runner.

    Returns None if any resolution fails; caller persists an error
    row and releases the claim.
    """
    chats = deps.storage_provider.get_storage(Chat)
    agents = deps.storage_provider.get_storage(Agent)
    msgs = deps.storage_provider.get_storage(ChatMessage)
    agent = await agents.get(chat.agent_id)
    if agent is None:
        return None
    try:
        llm = (
            deps.fake_llm
            if deps.fake_llm is not None
            else await deps.provider_registry.get_llm(agent.model.provider_id)
        )
    except (NotFoundError, ConfigError):
        return None
    provider_rows = deps.storage_provider.get_storage(LLMProvider)
    provider_row = await provider_rows.get(agent.model.provider_id)
    if provider_row is None:
        return None
    llm_model = next(
        (m for m in provider_row.models if m.name == agent.model.model_name),
        None,
    )
    if llm_model is None:
        return None
    toolset_ids: set[str] = set()
    for sid in (agent.tools or []):
        if "__" in sid:
            toolset_ids.add(sid.split("__", 1)[0])
    toolset_providers: dict[str, Any] = {}
    for tid in toolset_ids:
        try:
            toolset_providers[tid] = await deps.provider_registry.get_toolset(tid)
        except (NotFoundError, ConfigError):
            return None
    from matrix.agent.tool_manager import ToolExecutionManager
    tool_manager = ToolExecutionManager(
        toolset_providers=toolset_providers,
        provider_registry=deps.provider_registry,
        tools=agent.tools,
    )
    return ChatTurnRunner(
        agent=agent,
        llm=llm,
        llm_model=llm_model,
        tool_manager=tool_manager,
        chat_storage=chats,
        message_storage=msgs,
        cancel_event=cancel_event,
    )


async def _persist_build_error(
    deps: ChatDispatchDeps,
    chat: Chat,
    worker_id: str,
) -> None:
    chats = deps.storage_provider.get_storage(Chat)
    msgs = deps.storage_provider.get_storage(ChatMessage)
    next_seq = chat.last_seq + 1
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id(chat.id, next_seq),
        chat_id=chat.id, seq=next_seq, kind="error",
        payload={"message": "could not build chat runner",
                 "code": "runner_build_failed"},
        created_at=datetime.now(timezone.utc),
    ))
    chat.last_seq = next_seq
    await chats.update(chat)
    await deps.scheduler.release_chat(
        chat.id, worker_id, next_turn_status="idle",
    )
    await deps.event_bus.publish(
        f"chat:{chat.id}:tick", {"seq": next_seq},
    )


async def _heartbeat_loop(
    deps: ChatDispatchDeps,
    chat_id: str,
    worker_id: str,
    lease_lost: asyncio.Event,
) -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            ok = await deps.scheduler.heartbeat_chat(chat_id, worker_id)
            if not ok:
                lease_lost.set()
                return
    except asyncio.CancelledError:
        return


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
    """
    msgs = deps.storage_provider.get_storage(ChatMessage)
    pred = Predicate(
        left=FieldRef(name="chat_id"), op=Op.EQ,
        right=Value(value=chat_id),
    )

    _TERMINALS = frozenset({"done", "error", "cancelled", "yielded"})

    terminal_count = 0
    user_messages: list[ChatMessage] = []
    offset = 0
    PAGE = 200
    while True:
        page = await msgs.find(
            pred, OffsetPage(offset=offset, length=PAGE),
            order_by=[OrderBy(field="seq", direction="asc")],
        )
        for row in page.items:
            if row.kind in _TERMINALS:
                terminal_count += 1
            elif row.kind == "user_message":
                user_messages.append(row)
        if len(page.items) < PAGE:
            break
        offset += PAGE

    # The Nth terminal closes the Nth user_message; the (N+1)th
    # user_message is the next one to process. If there are fewer
    # user_messages than (terminal_count + 1), the queue is drained.
    target_index = terminal_count  # 0-based index into user_messages list
    if target_index < len(user_messages):
        return user_messages[target_index]
    return None


def _parts_from(user_message_row: ChatMessage) -> list:
    """Convert the persisted user_message payload back into Parts."""
    payload = user_message_row.payload or {}
    raw_parts = payload.get("parts")
    if isinstance(raw_parts, list) and raw_parts:
        from pydantic import TypeAdapter
        from matrix.model.chat import Part
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


__all__ = ["ChatDispatchDeps", "run_one_chat_turn"]
