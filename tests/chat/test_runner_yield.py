"""ChatTurnRunner must let YieldToWorker propagate out of run_turn so
the dispatch layer's park path can claim it. A swallowed yield would be
converted into a fake tool-error result, silently bypassing approval
gates and yielding-tool parks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    StreamEvent,
    ToolCallEnd,
    ToolCallStart,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker


class _FakeLLM:
    def __init__(self, stream_factory):
        self._stream_factory = stream_factory

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        return self._stream_factory()

    async def aclose(self):
        return None


class _YieldingToolManager:
    """list_tools returns one tool; execute parks via YieldToWorker."""

    async def list_tools(self, *, principal=None):
        return [{"name": "ask_user", "description": "d", "parameters": {}}]

    async def execute(self, call, *, principal=None, bypass_approval=False):
        raise YieldToWorker(
            Yielded(tool_name=call.name, event_key=f"ask_user:{call.id}"),
            tool_call_id=call.id,
        )


@pytest.mark.asyncio
async def test_yield_to_worker_propagates(fake_storage_provider):
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    chat = Chat(id="cy", agent_id="ag", created_at=datetime.now(timezone.utc))
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(chat)

    async def _tool_stream() -> AsyncIterator[StreamEvent]:
        yield ToolCallStart(id="tc1", name="ask_user", index=0)
        yield ToolCallEnd(id="tc1", arguments={"q": "ok?"}, index=0)
        yield Done(stop_reason="tool_use", raw_reason="tool_use")

    runner = ChatTurnRunner(
        agent=agent,
        llm=_FakeLLM(_tool_stream),
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=_YieldingToolManager(),
        chat_storage=chat_store,
        message_storage=msg_store,
    )

    rows: list[ChatMessage] = []
    with pytest.raises(YieldToWorker):
        async for r in runner.run_turn(chat, "go"):
            rows.append(r)

    # No fake tool-error result should have been persisted: the yield
    # must propagate, not be converted into an error tool_result row.
    errored = [
        r for r in rows
        if r.kind == "tool_result" and r.payload.get("error")
    ]
    assert not errored, f"yield was swallowed into a tool_result: {errored}"
