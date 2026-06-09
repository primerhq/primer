"""ChatTurnRunner must stop after agent.max_tool_turns tool rounds and
emit a terminal error row with code ``max_tool_turns_exceeded``.

A model that keeps emitting tool_use forever would otherwise loop
unbounded against the LLM."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    StreamEvent,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import LLMModel


class _AlwaysToolLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        self.calls += 1
        n = self.calls

        async def _gen() -> AsyncIterator[StreamEvent]:
            yield ToolCallStart(id=f"tc{n}", name="loop_tool", index=0)
            yield ToolCallEnd(id=f"tc{n}", arguments={}, index=0)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return _gen()

    async def aclose(self):
        return None


class _OkToolManager:
    def __init__(self) -> None:
        self.executed = 0

    async def list_tools(self, *, principal=None):
        return [{"name": "loop_tool", "description": "d", "parameters": {}}]

    async def execute(self, call, *, principal=None, bypass_approval=False):
        self.executed += 1
        return ToolResultPart(id=call.id, output="ok", error=False)


@pytest.mark.asyncio
async def test_chat_runner_stops_at_max_tool_turns(fake_storage_provider) -> None:
    agent = Agent(
        id="ag",
        description="x",
        model=AgentModel(provider_id="p", model_name="m"),
        max_tool_turns=3,
    )
    chat = Chat(id="cmt", agent_id="ag", created_at=datetime.now(timezone.utc))
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    tm = _OkToolManager()
    llm = _AlwaysToolLLM()
    runner = ChatTurnRunner(
        agent=agent,
        llm=llm,
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=tm,
        chat_storage=chat_store,
        message_storage=msg_store,
    )

    rows: list[ChatMessage] = []

    async def _drive() -> None:
        async for r in runner.run_turn(chat, "go"):
            rows.append(r)

    await asyncio.wait_for(_drive(), timeout=5.0)

    # cap=3: dispatch on rounds 1 and 2, stop before round 3.
    assert tm.executed == 2
    cap_rows = [
        r for r in rows
        if r.kind == "error"
        and r.payload.get("code") == "max_tool_turns_exceeded"
    ]
    assert len(cap_rows) == 1
    assert "3" in cap_rows[0].payload["message"]
