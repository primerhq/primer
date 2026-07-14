"""Tool access during compaction (``Agent.compaction_tool_access``).

Exercises ``CompactionStrategy``'s tier-2 tool-use loop directly with a
scripted fake LLM + a fake tool manager -- no executor / workspace needed.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.provider import LLMModel


# --- fakes + helpers --------------------------------------------------------


def _agent(**kw: Any) -> Agent:
    return Agent(
        id="researcher",
        description="Research agent",
        model=AgentModel(provider_id="openai-1", model_name="gpt-4o-mini"),
        **kw,
    )


def _model(context_length: int = 1000) -> LLMModel:
    return LLMModel(name="gpt-4o-mini", context_length=context_length)


def _u(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _a(text: str) -> Message:
    return Message(role="assistant", parts=[TextPart(text=text)])


# 6 turns; with tail_turns=1 the head is non-empty so tier-2 summarisation runs.
HISTORY = [m for i in range(1, 7) for m in (_u(f"q{i}"), _a(f"a{i}"))]


class _ScriptedLLM:
    """Yields a distinct scripted event list per ``stream()`` call."""

    def __init__(self, rounds: list[list[StreamEvent]]) -> None:
        self._rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []

    def stream(
        self, *, model: str, messages: list[Message], **kwargs: Any
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": list(messages), **kwargs})
        script = (
            self._rounds.pop(0)
            if self._rounds
            else [Done(stop_reason="stop", raw_reason="stop")]
        )

        async def _gen() -> AsyncIterator[StreamEvent]:
            for ev in script:
                yield ev

        return _gen()


class _FakeToolManager:
    def __init__(self, *, raise_on: str | None = None) -> None:
        self._raise_on = raise_on
        self.executed: list[ToolCallPart] = []

    async def list_tools(self, *, principal: str | None = None) -> list[Tool]:
        return []  # the scripted LLM emits calls regardless of the catalogue

    async def execute(
        self, call: ToolCallPart, *, principal: str | None = None
    ) -> ToolResultPart:
        self.executed.append(call)
        if self._raise_on and call.name == self._raise_on:
            raise RuntimeError("boom")
        return ToolResultPart(id=call.id, output=f"wrote {call.arguments}", error=False)


def _tool_round(cid: str, name: str, args: dict) -> list[StreamEvent]:
    return [
        ToolCallStart(id=cid, name=name, index=0),
        ToolCallEnd(id=cid, arguments=args, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


def _text_round(text: str) -> list[StreamEvent]:
    return [TextDelta(text=text, index=0), Done(stop_reason="stop", raw_reason="stop")]


def _strategy() -> CompactionStrategy:
    return CompactionStrategy(tail_turns=1)


async def _run(llm, tm, *, max_tool_turns=None, agent=None):
    events: list[StreamEvent] = []

    async def sink(ev: StreamEvent) -> None:
        events.append(ev)

    result = await _strategy().force_compact(
        agent=agent or _agent(),
        llm=llm,
        model=_model(),
        history=list(HISTORY),
        tool_manager=tm,
        event_sink=sink,
        max_tool_turns=max_tool_turns,
    )
    return result, events


def _summary_text(result) -> str:
    assert result.summary_message is not None
    return "".join(
        p.text for p in result.summary_message.parts if isinstance(p, TextPart)
    )


# --- tests ------------------------------------------------------------------


async def test_tool_call_executed_and_final_text_is_summary() -> None:
    llm = _ScriptedLLM(
        [
            _tool_round("c1", "workspace__write", {"path": "dump.md"}),
            _text_round("wrote the dump to dump.md"),
        ]
    )
    tm = _FakeToolManager()
    result, events = await _run(llm, tm)

    assert [c.name for c in tm.executed] == ["workspace__write"]
    assert "wrote the dump to dump.md" in _summary_text(result)
    # The intermediate tool call/result must NOT leak into the compacted history.
    assert not any(m.role == "tool" for m in result.new_messages)
    assert not any(
        isinstance(p, ToolCallPart)
        for m in result.new_messages
        for p in m.parts
    )
    # Tools were threaded to the LLM call, and the second call carried the
    # tool result back (loop actually iterated).
    assert "tools" in llm.calls[0]
    assert len(llm.calls) == 2
    # Tool activity surfaced to the sink.
    assert any(isinstance(e, ToolCallStart) for e in events)
    assert any(isinstance(e, ExtendedEvent) for e in events)


async def test_no_tool_manager_is_text_only_unchanged() -> None:
    llm = _ScriptedLLM([_text_round("plain summary")])
    result = await _strategy().force_compact(
        agent=_agent(), llm=llm, model=_model(), history=list(HISTORY),
    )
    assert "plain summary" in _summary_text(result)
    # The text-only path passes no tools.
    assert not llm.calls[0].get("tools")
    assert len(llm.calls) == 1


async def test_empty_final_text_falls_back_to_marker() -> None:
    llm = _ScriptedLLM(
        [
            _tool_round("c1", "workspace__write", {"path": "x"}),
            [Done(stop_reason="stop", raw_reason="stop")],  # no text at all
        ]
    )
    result, _ = await _run(llm, _FakeToolManager())
    assert "delegated the detail to tools" in _summary_text(result)


async def test_tool_failure_is_fed_back_and_loop_continues() -> None:
    llm = _ScriptedLLM(
        [
            _tool_round("c1", "workspace__write", {"path": "x"}),
            _text_round("finished despite the tool error"),
        ]
    )
    tm = _FakeToolManager(raise_on="workspace__write")
    result, events = await _run(llm, tm)
    assert "finished despite the tool error" in _summary_text(result)
    # The failure surfaced as an error tool-result event, and the loop did not abort.
    assert any(
        isinstance(e, ExtendedEvent) and getattr(e.extended, "error", False)
        for e in events
    )


def test_agent_flag_defaults_off_and_round_trips() -> None:
    assert _agent().compaction_tool_access is False
    assert _agent(compaction_tool_access=True).compaction_tool_access is True


async def test_tool_turn_cap_enforced() -> None:
    # Every round emits a tool call; without a cap this never terminates.
    rounds = [_tool_round(f"c{i}", "workspace__write", {"n": i}) for i in range(10)]
    llm = _ScriptedLLM(rounds)
    tm = _FakeToolManager()
    result, _ = await _run(llm, tm, max_tool_turns=2)
    assert 1 <= len(tm.executed) <= 2  # bounded well below the 10 scripted rounds
    assert result.summary_message is not None
