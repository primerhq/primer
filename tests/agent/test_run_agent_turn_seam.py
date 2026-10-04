"""run_agent_turn's ``tools=`` and ``budget=`` seam.

Both default to "nothing": the loop fetches its own catalogue and sends every
prompt as it built it. Passing them must not change what is persisted: the
guard sees and returns only the OUTGOING prompt, never ``messages_out``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
    Usage,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096,
    config=ModelProfileConfig(),
)
AGENT = Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=10)
TOOL = Tool(id="loop_tool", description="d", toolset_id="t",
            args_schema={"type": "object", "properties": {}})


class _RoundsLLM:
    """``tool_rounds`` rounds that call a tool, then one that answers. Records what it was sent."""

    def __init__(self, tool_rounds: int = 1) -> None:
        self.tool_rounds = tool_rounds
        self.sent: list[dict] = []

    def stream(self, *, model, messages, tools=None, **kwargs):
        self.sent.append({"messages": list(messages), "tools": tools})
        n = len(self.sent)

        async def _gen() -> AsyncIterator[StreamEvent]:
            if n <= self.tool_rounds:
                yield ToolCallStart(id=f"tc{n}", name="loop_tool", index=0)
                yield ToolCallEnd(id=f"tc{n}", arguments={}, index=0)
                yield Usage(input_tokens=111 * n, output_tokens=5, cumulative=False)
                yield Done(stop_reason="tool_use", raw_reason="tool_use")
            else:
                yield TextDelta(text="done", index=0)
                yield Usage(input_tokens=222 * n, output_tokens=7, cumulative=False)
                yield Done(stop_reason="stop", raw_reason="stop")

        return _gen()


class _Manager:
    def __init__(self, *, listing_allowed: bool = True) -> None:
        self.listings = 0
        self._listing_allowed = listing_allowed

    def is_notifying(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal=None):
        self.listings += 1
        if not self._listing_allowed:
            raise AssertionError("list_tools must not be called when tools= is given")
        return [TOOL]

    async def execute(self, call, *, principal=None):
        return ToolResultPart(id=call.id, output="RAW-" + "r" * 100, error=False)


async def _drive(tool_rounds: int = 1, **kwargs) -> tuple[_RoundsLLM, list[Message]]:
    llm = _RoundsLLM(tool_rounds)
    messages_out: list[Message] = []
    async for _ in run_agent_turn(
        agent=AGENT, llm=llm, llm_model=MODEL, tool_manager=kwargs.pop("tool_manager", _Manager()),
        prompt=[Message(role="user", parts=[TextPart(text="go")])],
        messages_out=messages_out, **kwargs,
    ):
        pass
    return llm, messages_out


class TestTools:
    async def test_by_default_the_loop_fetches_the_catalogue_itself(self) -> None:
        manager = _Manager()
        llm, _ = await _drive(tool_manager=manager)
        assert manager.listings == 1
        assert llm.sent[0]["tools"] == [TOOL]

    async def test_a_catalogue_passed_in_is_used_and_never_fetched_again(self) -> None:
        manager = _Manager(listing_allowed=False)
        passed = [TOOL]
        llm, _ = await _drive(tool_manager=manager, tools=passed)
        assert manager.listings == 0
        assert [call["tools"] for call in llm.sent] == [passed, passed]


class _Guard:
    """Records every call and prunes tool results in the outgoing prompt."""

    def __init__(self) -> None:
        self.before: list[list[Message]] = []
        self.before_tools: list[list[Tool]] = []
        self.usages: list = []

    async def before_call(self, prompt, *, tools):
        self.before.append(list(prompt))
        self.before_tools.append(tools)
        return [
            m.model_copy(update={"parts": [
                p.model_copy(update={"output": "PRUNED"}) if isinstance(p, ToolResultPart) else p
                for p in m.parts
            ]})
            for m in prompt
        ]

    def after_call(self, usage) -> None:
        self.usages.append(usage)


class TestBudget:
    async def test_the_guard_runs_before_every_call_the_first_included(self) -> None:
        guard = _Guard()
        llm, _ = await _drive(budget=guard)
        assert len(guard.before) == len(llm.sent) == 2
        assert [len(p) for p in guard.before] == [1, 3], "round 2 sees the accumulated prompt"
        assert guard.before_tools == [[TOOL], [TOOL]]

    async def test_what_the_guard_returns_is_what_the_model_is_sent(self) -> None:
        guard = _Guard()
        llm, _ = await _drive(budget=guard)
        round2 = llm.sent[1]["messages"]
        result = next(p for m in round2 if m.role == "tool" for p in m.parts)
        assert result.output == "PRUNED"

    async def test_the_persisted_messages_stay_raw(self) -> None:
        """messages_out is what the caller persists and what a park stamps: a
        guard's reduction is ephemeral and the durable record is never reduced."""
        _, messages_out = await _drive(budget=_Guard())
        results = [p for m in messages_out if m.role == "tool" for p in m.parts]
        assert results and results[0].output.startswith("RAW-")

    async def test_the_loops_own_prompt_is_not_replaced_by_the_guards_output(self) -> None:
        """A guard must re-derive on every call; the loop keeps accumulating the
        unreduced prompt, so a later call's input is not an earlier call's output.
        Needs two tool rounds: round 3's prompt holds a result the guard already
        saw (and pruned) in round 2."""
        guard = _Guard()
        await _drive(tool_rounds=2, budget=guard)
        round3 = [p for m in guard.before[2] if m.role == "tool" for p in m.parts]
        assert len(round3) == 2
        assert all(p.output.startswith("RAW-") for p in round3)

    async def test_after_call_receives_each_calls_usage(self) -> None:
        guard = _Guard()
        await _drive(budget=guard)
        assert [(u.input_tokens, u.output_tokens) for u in guard.usages] == [(111, 5), (444, 7)]

    async def test_without_a_guard_the_prompt_is_sent_unchanged(self) -> None:
        llm, messages_out = await _drive()
        result = next(p for m in llm.sent[1]["messages"] if m.role == "tool" for p in m.parts)
        assert result.output.startswith("RAW-")
        assert [m.role for m in messages_out] == ["assistant", "tool", "assistant"]
