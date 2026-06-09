"""run_agent_turn must stop after agent.max_tool_turns tool-call rounds.

A model that keeps emitting tool calls forever would otherwise loop
unbounded. The cap force-stops the turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextPart,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.provider import LLMModel


class _AlwaysToolLLM:
    """Emits one tool call on every stream, never a plain stop."""

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
    """list_tools returns one tool; execute always returns a normal result."""

    def __init__(self) -> None:
        self.executed = 0

    async def list_tools(self, *, principal=None):
        return [{"name": "loop_tool", "description": "d", "parameters": {}}]

    async def execute(self, call, *, principal=None):
        self.executed += 1
        return ToolResultPart(id=call.id, output="ok", error=False)


def test_agent_max_tool_turns_default_and_none() -> None:
    a = Agent(
        id="ag",
        description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    assert a.max_tool_turns == 50
    b = Agent(
        id="ag2",
        description="x",
        model=AgentModel(provider_id="p", model_name="m"),
        max_tool_turns=None,
    )
    assert b.max_tool_turns is None


@pytest.mark.asyncio
async def test_run_agent_turn_stops_at_max_tool_turns() -> None:
    agent = Agent(
        id="ag",
        description="x",
        model=AgentModel(provider_id="p", model_name="m"),
        max_tool_turns=3,
    )
    llm = _AlwaysToolLLM()
    tm = _OkToolManager()
    messages_out: list[Message] = []

    async def _drive() -> None:
        async for _ in run_agent_turn(
            agent=agent,
            llm=llm,
            llm_model=LLMModel(name="m", context_length=4096),
            tool_manager=tm,
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
            messages_out=messages_out,
        ):
            pass

    # Hard safety bound: a regression that loops forever fails fast.
    await asyncio.wait_for(_drive(), timeout=5.0)

    # With cap=3 the counter hits 3 on the third LLM round and the loop
    # stops BEFORE dispatching that round's tools: 3 LLM calls, 2 dispatches.
    assert llm.calls == 3
    assert tm.executed == 2
