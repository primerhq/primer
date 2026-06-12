from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from primer.chat.dispatch import ChatDispatchDeps, _apply_switch_handoff
from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, StreamEvent, ToolCallEnd, ToolCallStart
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker


def _now():
    return datetime.now(timezone.utc)


class _Chats:
    def __init__(self, stored): self.stored = stored; self.updated = None
    async def get(self, cid): return self.stored
    async def update(self, chat): self.updated = chat.model_copy(deep=True); self.stored = self.updated


@pytest.mark.asyncio
async def test_handle_switch_sets_agent_and_queues_handoff():
    chat = Chat(id="c1", agent_id="agent-A", created_at=_now(), last_seq=2)
    chats = _Chats(chat.model_copy(deep=True))
    runner = ChatTurnRunner.__new__(ChatTurnRunner)
    runner._chats = chats
    exc = YieldToWorker(
        Yielded(tool_name="switch_to_agent", event_key="",
                resume_metadata={"agent_id": "agent-B", "prompt": "take over: do X"}),
        tool_call_id="tc1")
    await runner.handle_switch(chat, exc)
    assert chat.agent_id == "agent-B"
    assert chat.pending_handoff == "take over: do X"
    assert chat.pending_tool_call is None


class _FakeLLM:
    def __init__(self, stream_factory):
        self._stream_factory = stream_factory

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        return self._stream_factory()

    async def aclose(self):
        return None


class _SwitchingToolManager:
    """execute parks via YieldToWorker stamped with switch_to_agent."""

    async def list_tools(self, *, principal=None):
        return [{"name": "switch_to_agent", "description": "d", "parameters": {}}]

    async def execute(self, call, *, principal=None, bypass_approval=False):
        raise YieldToWorker(
            Yielded(
                tool_name="switch_to_agent",
                event_key=f"switch_to_agent:{call.id}",
                resume_metadata={"agent_id": "agent-B", "prompt": "do X"},
            ),
            tool_call_id=call.id,
        )


@pytest.mark.asyncio
async def test_switch_yield_propagates_out_of_run_turn(fake_storage_provider):
    """The executor MUST let a switch_to_agent YieldToWorker propagate out of
    run_turn (gate at executor.py:601) instead of swallowing it into an inline
    'not supported on the chat surface' tool_error. A swallowed switch would
    make switch_to_agent dead code in real chats."""
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    chat = Chat(id="cs", agent_id="ag", created_at=_now())
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    async def _tool_stream() -> AsyncIterator[StreamEvent]:
        yield ToolCallStart(id="tc1", name="switch_to_agent", index=0)
        yield ToolCallEnd(id="tc1", arguments={"agent_id": "agent-B", "prompt": "do X"}, index=0)
        yield Done(stop_reason="tool_use", raw_reason="tool_use")

    runner = ChatTurnRunner(
        agent=agent,
        llm=_FakeLLM(_tool_stream),
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=_SwitchingToolManager(),
        chat_storage=chat_store,
        message_storage=msg_store,
    )

    rows: list[ChatMessage] = []
    with pytest.raises(YieldToWorker) as ei:
        async for r in runner.run_turn(chat, "go"):
            rows.append(r)

    assert ei.value.yielded.tool_name == "switch_to_agent"
    errored = [
        r for r in rows
        if r.kind == "tool_result" and r.payload.get("error")
    ]
    assert not errored, f"switch yield was swallowed into a tool_result: {errored}"


@pytest.mark.asyncio
async def test_apply_switch_handoff_returns_claimable_and_queues_handoff(
    fake_storage_provider,
):
    """The dispatch helper ends the turn by switching the agent and queuing the
    handoff prompt as the next user_message, releasing 'claimable' so a fresh
    claim runs the new agent."""
    chat = Chat(id="cd", agent_id="agent-A", created_at=_now(), last_seq=4)
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
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
            resume_metadata={"agent_id": "agent-B", "prompt": "take over: do X"},
        ),
        tool_call_id="tc1",
    )

    status = await _apply_switch_handoff(runner, chat, exc, deps)

    assert status == "claimable"
    stored = await chat_store.get("cd")
    assert stored.agent_id == "agent-B"
    assert stored.pending_handoff is None  # consumed by the injection
    user_msgs = [
        m for m in await _all_messages(msg_store, "cd")
        if m.kind == "user_message"
    ]
    assert any(
        "take over: do X" in _flat(m) for m in user_msgs
    ), f"handoff prompt was not queued: {user_msgs}"


async def _all_messages(msg_store, chat_id):
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
    return list(page.items)


def _flat(msg: ChatMessage) -> str:
    parts = msg.payload.get("parts") or []
    return " ".join(
        p.get("text", "") for p in parts if isinstance(p, dict)
    )
