"""Task A4 (chat-refactor plan): stamp the producing agent on every row
``ChatTurnRunner`` writes, and add the ``agent_marker`` kind the
redesigned agent-timeline UI reads for per-turn attribution.

Covers:
* every ``assistant_token`` / ``tool_call`` / ``tool_result`` / ``done``
  row carries ``payload["agent_id"] == <producing agent id>``.
* the ``user_message`` row (persisted outside ``_append``) carries no
  ``agent_id``.
* ``"agent_marker"`` is a legal :data:`ChatMessageKind`.
* :meth:`ChatTurnRunner._load_history` drops ``agent_marker`` rows
  entirely — they're legibility markers, not model history.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import get_args

import pytest

from primer.chat.enqueue import append_user_message
from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.chats import Chat, ChatMessage, ChatMessageKind
from primer.model.provider import LLMModel


def _now():
    return datetime.now(timezone.utc)


class _FakeLLM:
    """Replays one stream factory per ``stream(...)`` call, in order."""

    def __init__(self, stream_factories):
        self._factories = list(stream_factories)

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        return self._factories.pop(0)()

    async def aclose(self):
        return None


class _EchoToolManager:
    """One real (non-yielding) tool call round-trip."""

    async def list_tools(self, *, principal=None):
        return [{"name": "do_thing", "description": "d", "parameters": {}}]

    async def execute(self, call, *, principal=None, bypass_approval=False):
        return ToolResultPart(id=call.id, output="ok", error=False)


async def _turn_with_tool_call() -> AsyncIterator[StreamEvent]:
    yield TextDelta(text="thinking...", index=0)
    yield ToolCallStart(id="tc1", name="do_thing", index=0)
    yield ToolCallEnd(id="tc1", arguments={}, index=0)
    yield Done(stop_reason="tool_use", raw_reason="tool_use")


async def _final_turn() -> AsyncIterator[StreamEvent]:
    yield TextDelta(text="done!", index=0)
    yield Done(stop_reason="stop", raw_reason="stop")


def _build_runner(chat_store, msg_store, *, agent_id="ag-1") -> ChatTurnRunner:
    agent = Agent(
        id=agent_id, description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    return ChatTurnRunner(
        agent=agent,
        llm=_FakeLLM([_turn_with_tool_call, _final_turn]),
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=_EchoToolManager(),
        chat_storage=chat_store,
        message_storage=msg_store,
    )


@pytest.mark.asyncio
async def test_every_agent_produced_row_carries_agent_id(fake_storage_provider):
    chat = Chat(id="c1", agent_id="ag-1", created_at=_now())
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    # Mirror the real dispatch flow (primer/chat/dispatch.py:run_one_chat_turn):
    # the user_message is pre-persisted via the enqueue helper (NOT
    # ChatTurnRunner._append) and handed to run_turn as
    # already_persisted_user_msg, so it must NOT be stamped with agent_id.
    user_msg = await append_user_message(
        chat=chat,
        parts=[TextPart(text="hello")],
        storage_provider=fake_storage_provider,
    )

    runner = _build_runner(chat_store, msg_store, agent_id="ag-1")

    rows: list[ChatMessage] = []
    async for row in runner.run_turn(
        chat, [TextPart(text="hello")], already_persisted_user_msg=user_msg,
    ):
        rows.append(row)

    kinds = [r.kind for r in rows]
    # Sanity: the full round-trip (assistant text -> tool_call ->
    # tool_result -> assistant text -> done) actually happened, so the
    # assertions below exercise every kind named in the plan.
    assert "assistant_token" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "done" in kinds

    user_rows = [r for r in rows if r.kind == "user_message"]
    assert len(user_rows) == 1
    assert "agent_id" not in user_rows[0].payload

    agent_rows = [r for r in rows if r.kind != "user_message"]
    assert agent_rows
    for row in agent_rows:
        assert row.payload.get("agent_id") == "ag-1", row


def test_agent_marker_is_a_legal_chat_message_kind():
    assert "agent_marker" in get_args(ChatMessageKind)


@pytest.mark.asyncio
async def test_load_history_drops_agent_marker_rows(fake_storage_provider):
    chat = Chat(id="c2", agent_id="ag-1", created_at=_now())
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    now = _now()
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c2", 1), chat_id="c2", seq=1,
        kind="user_message", payload={"content": "hi"}, created_at=now,
    ))
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c2", 2), chat_id="c2", seq=2,
        kind="agent_marker",
        payload={"marker": "switch", "agent_id": "ag-2", "from_agent_id": "ag-1"},
        created_at=now,
    ))
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c2", 3), chat_id="c2", seq=3,
        kind="assistant_token",
        payload={"delta": "hello", "agent_id": "ag-2"}, created_at=now,
    ))

    runner = _build_runner(chat_store, msg_store, agent_id="ag-2")
    history = await runner._load_history("c2")

    # Only the user_message and assistant_token rows materialize into
    # history; the agent_marker row must not leak in as a spurious
    # Message (it carries no model-visible content).
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[1].role == "assistant"


@pytest.mark.asyncio
async def test_dispatch_terminals_and_next_user_message_unaffected_by_agent_marker(
    fake_storage_provider,
):
    """Regression per Task A4: `_TERMINALS` and `_find_next_user_message`
    treat `agent_marker` as neither a terminal nor a user_message, so an
    agent_marker row sitting between a user_message and its terminal must
    not be mistaken for either."""
    from primer.chat.dispatch import ChatDispatchDeps, _find_next_user_message

    chat = Chat(id="c3", agent_id="ag-1", created_at=_now())
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    now = _now()
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c3", 1), chat_id="c3", seq=1,
        kind="user_message", payload={"content": "hi"}, created_at=now,
    ))
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c3", 2), chat_id="c3", seq=2,
        kind="agent_marker",
        payload={"marker": "handoff", "agent_id": "ag-2", "from_agent_id": "ag-1"},
        created_at=now,
    ))
    chat.last_seq = 2
    await chat_store.update(chat)

    deps = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=None,
        event_bus=None,
        chat_tick_router=None,
    )
    # The user_message has no terminal yet (the agent_marker doesn't count
    # as one) -> it must still be found as the next unprocessed turn.
    next_um = await _find_next_user_message(deps, "c3")
    assert next_um is not None
    assert next_um.seq == 1


def test_message_to_wire_passes_agent_marker_through_unchanged():
    """`_message_to_wire` only special-cases `compaction_marker`; an
    `agent_marker` row must merge its payload to the top level like every
    other ordinary kind, NOT be caught by the compaction branch."""
    from primer.api.routers.chats import _message_to_wire

    row = ChatMessage(
        id=ChatMessage.make_id("c4", 1), chat_id="c4", seq=1,
        kind="agent_marker",
        payload={"marker": "switch", "agent_id": "ag-2", "from_agent_id": "ag-1"},
        created_at=_now(),
    )
    wire = _message_to_wire(row)
    assert wire["kind"] == "agent_marker"
    assert wire["seq"] == 1
    assert wire["marker"] == "switch"
    assert wire["agent_id"] == "ag-2"
    assert wire["from_agent_id"] == "ag-1"
    # The compaction envelope shape (e.g. a `summary` key) must NOT appear.
    assert "summary" not in wire
