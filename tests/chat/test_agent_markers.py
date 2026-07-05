"""Task A5 (chat-refactor plan): emit ``agent_marker`` rows at each
attribution boundary — an operator-driven switch
(``POST /v1/chats/{id}/agent``) or a ``switch_to_agent`` tool handoff
(``_apply_switch_handoff`` in :mod:`primer.chat.dispatch``).

Covers:
* the shared helper :func:`primer.chat.enqueue.append_agent_marker`
  persists an ``agent_marker`` row and bumps ``chat.last_seq``.
* ``switch_chat_agent`` (the REST endpoint) appends exactly one
  ``agent_marker{marker:"switch"}`` row with the correct from/to agent
  ids, and publishes a ``chat:{id}:tick``.
* ``_apply_switch_handoff`` (the ``switch_to_agent`` tool path) appends
  an ``agent_marker{marker:"handoff"}`` row with the correct from/to
  agent ids, and publishes a ``chat:{id}:tick``.
* :meth:`primer.chat.executor.ChatTurnRunner._load_history` still drops
  both marker shapes — no history pollution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.chat.dispatch import ChatDispatchDeps, _apply_switch_handoff
from primer.chat.enqueue import append_agent_marker
from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker


def _now():
    return datetime.now(timezone.utc)


async def _all_marker_rows(msg_store, chat_id) -> list[ChatMessage]:
    from primer.model.storage import (
        FieldRef, OffsetPage, Op, OrderBy, Predicate, Value,
    )
    pred = Predicate(
        left=FieldRef(name="chat_id"), op=Op.EQ, right=Value(value=chat_id),
    )
    page = await msg_store.find(
        pred, OffsetPage(offset=0, length=200),
        order_by=[OrderBy(field="seq", direction="asc")],
    )
    return [m for m in page.items if m.kind == "agent_marker"]


# ===========================================================================
# append_agent_marker — the shared helper
# ===========================================================================


@pytest.mark.asyncio
async def test_append_agent_marker_persists_row_and_bumps_last_seq(
    fake_storage_provider,
):
    chat = Chat(id="c1", agent_id="ag-1", created_at=_now(), last_seq=3)
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    row = await append_agent_marker(
        chat, fake_storage_provider,
        marker="switch", agent_id="ag-2", from_agent_id="ag-1",
    )

    assert row.kind == "agent_marker"
    assert row.seq == 4
    assert row.payload == {
        "marker": "switch", "agent_id": "ag-2", "from_agent_id": "ag-1",
    }
    assert chat.last_seq == 4
    stored = await chat_store.get("c1")
    assert stored.last_seq == 4
    rows = await _all_marker_rows(msg_store, "c1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_append_agent_marker_omits_from_agent_id_when_none(
    fake_storage_provider,
):
    chat = Chat(id="c1b", agent_id="ag-1", created_at=_now())
    chat_store = fake_storage_provider.get_storage(Chat)
    await chat_store.create(chat)

    row = await append_agent_marker(
        chat, fake_storage_provider, marker="switch", agent_id="ag-2",
    )
    assert "from_agent_id" not in row.payload


# ===========================================================================
# switch_chat_agent (REST endpoint) — marker="switch"
# ===========================================================================


@pytest.mark.asyncio
async def test_switch_chat_agent_endpoint_appends_switch_marker_and_ticks(
    fake_storage_provider,
):
    from primer.api.routers.chats import ChatSwitchAgentBody, switch_chat_agent

    chat_store = fake_storage_provider.get_storage(Chat)
    agent_store = fake_storage_provider.get_storage(Agent)
    msg_store = fake_storage_provider.get_storage(ChatMessage)

    await agent_store.create(Agent(
        id="ag-1", description="a",
        model=AgentModel(provider_id="p", model_name="m"),
    ))
    await agent_store.create(Agent(
        id="ag-2", description="b",
        model=AgentModel(provider_id="p", model_name="m"),
    ))
    chat = Chat(id="c2", agent_id="ag-1", created_at=_now())
    await chat_store.create(chat)

    bus = InMemoryEventBus()
    await bus.initialize()
    sub = bus.subscribe()
    fake_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(event_bus=bus)),
    )

    result = await switch_chat_agent(
        ChatSwitchAgentBody(agent_id="ag-2"),
        fake_request,
        chat_id="c2",
        sp=fake_storage_provider,
        agents=agent_store,
    )
    assert result.agent_id == "ag-2"

    rows = await _all_marker_rows(msg_store, "c2")
    assert len(rows) == 1
    assert rows[0].payload == {
        "marker": "switch", "agent_id": "ag-2", "from_agent_id": "ag-1",
    }

    event = await sub.__anext__()
    assert event.event_key == "chat:c2:tick"
    assert event.payload["seq"] == rows[0].seq
    await sub.aclose()
    await bus.aclose()


# ===========================================================================
# _apply_switch_handoff (switch_to_agent tool path) — marker="handoff"
# ===========================================================================


@pytest.mark.asyncio
async def test_apply_switch_handoff_appends_handoff_marker_and_ticks(
    fake_storage_provider,
):
    chat = Chat(id="c3", agent_id="agent-A", created_at=_now(), last_seq=4)
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    runner = ChatTurnRunner.__new__(ChatTurnRunner)
    runner._chats = chat_store

    bus = InMemoryEventBus()
    await bus.initialize()
    sub = bus.subscribe()
    deps = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=None,
        event_bus=bus,
        chat_tick_router=None,
    )
    exc = YieldToWorker(
        Yielded(
            tool_name="switch_to_agent", event_key="",
            resume_metadata={"agent_id": "agent-B", "prompt": "take over: do X"},
        ),
        tool_call_id="tc1",
    )

    status = await _apply_switch_handoff(runner, chat, exc, deps)
    assert status == "claimable"

    rows = await _all_marker_rows(msg_store, "c3")
    assert len(rows) == 1
    assert rows[0].payload == {
        "marker": "handoff", "agent_id": "agent-B", "from_agent_id": "agent-A",
    }

    event = await sub.__anext__()
    assert event.event_key == "chat:c3:tick"
    assert event.payload["seq"] == rows[0].seq
    await sub.aclose()
    await bus.aclose()


@pytest.mark.asyncio
async def test_apply_switch_handoff_tolerates_missing_event_bus(
    fake_storage_provider,
):
    """A ``None`` event_bus (as some pre-A5 unit tests wire up) must not
    crash the tick publish — mirrors the optional-bus guard used
    elsewhere (e.g. ``compact_chat``'s REST handler)."""
    chat = Chat(id="c4", agent_id="agent-A", created_at=_now(), last_seq=1)
    chat_store = fake_storage_provider.get_storage(Chat)
    await chat_store.create(chat)

    runner = ChatTurnRunner.__new__(ChatTurnRunner)
    runner._chats = chat_store

    deps = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=None,
        event_bus=None,
        chat_tick_router=None,
    )
    exc = YieldToWorker(
        Yielded(
            tool_name="switch_to_agent", event_key="",
            resume_metadata={"agent_id": "agent-B", "prompt": "do X"},
        ),
        tool_call_id="tc1",
    )

    status = await _apply_switch_handoff(runner, chat, exc, deps)
    assert status == "claimable"


# ===========================================================================
# _load_history still drops both marker shapes
# ===========================================================================


def _build_runner_for_history(chat_store, msg_store, *, agent_id) -> ChatTurnRunner:
    agent = Agent(
        id=agent_id, description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    runner = ChatTurnRunner.__new__(ChatTurnRunner)
    runner._agent = agent
    runner._llm = None
    runner._model = LLMModel(name="m", context_length=4096)
    runner._tools = None
    runner._chats = chat_store
    runner._messages = msg_store
    runner._cancel_event = None
    runner._marker_persisted = False
    runner._last_input_tokens = None
    runner._last_output_tokens = None
    return runner


@pytest.mark.asyncio
async def test_load_history_drops_switch_and_handoff_markers(
    fake_storage_provider,
):
    chat = Chat(id="c5", agent_id="ag-2", created_at=_now())
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    now = _now()
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c5", 1), chat_id="c5", seq=1,
        kind="user_message", payload={"content": "hi"}, created_at=now,
    ))
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c5", 2), chat_id="c5", seq=2,
        kind="agent_marker",
        payload={"marker": "switch", "agent_id": "ag-2", "from_agent_id": "ag-1"},
        created_at=now,
    ))
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c5", 3), chat_id="c5", seq=3,
        kind="agent_marker",
        payload={"marker": "handoff", "agent_id": "ag-3", "from_agent_id": "ag-2"},
        created_at=now,
    ))
    await msg_store.create(ChatMessage(
        id=ChatMessage.make_id("c5", 4), chat_id="c5", seq=4,
        kind="assistant_token",
        payload={"delta": "hello", "agent_id": "ag-3"}, created_at=now,
    ))

    runner = _build_runner_for_history(chat_store, msg_store, agent_id="ag-3")
    history = await runner._load_history("c5")

    assert len(history) == 2
    assert history[0].role == "user"
    assert history[1].role == "assistant"
