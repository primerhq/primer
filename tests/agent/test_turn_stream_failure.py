"""01a070d6: run_agent_turn raises TurnStreamFailure instead of quietly
returning when a stream ends in a terminal Error.

Covers the three failure shapes (zero-content, partial-content-then-error,
and - since D-A3-03 - a zero-content stream that ends in Done rather than
Error: the model said nothing, reasoning-only or otherwise, and the turn
must fail with the ended_detail instead of resting as a silent success).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Error,
    Message,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    TextPart,
    TurnStreamFailure,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel


def _agent() -> Agent:
    return Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"))


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="test-profile", provider_id="test-provider",
        model_name="m", context_length=4096, config=ModelProfileConfig(),
    )


class _NoToolManager:
    def is_notifying(self, tool_name: str) -> bool:
        del tool_name
        return False

    async def list_tools(self, *, principal=None):
        return []

    async def execute(self, call, *, principal=None):
        raise AssertionError("no tool call expected in these scenarios")


class _ScriptedLLM:
    """Replays one fixed event list per call, advancing through a script."""

    def __init__(self, *rounds: list[StreamEvent]) -> None:
        self._rounds = list(rounds)
        self.calls = 0

    def stream(self, *, model, messages, **kwargs):
        events = self._rounds[self.calls]
        self.calls += 1

        async def _gen() -> AsyncIterator[StreamEvent]:
            for ev in events:
                yield ev

        return _gen()

    async def aclose(self):
        return None


async def _drive(llm, *, messages_out=None) -> list[StreamEvent]:
    seen: list[StreamEvent] = []

    async def _run() -> None:
        async for ev in run_agent_turn(
            agent=_agent(), llm=llm, llm_model=_model(),
            tool_manager=_NoToolManager(),
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
            messages_out=messages_out,
        ):
            seen.append(ev)

    await asyncio.wait_for(_run(), timeout=5.0)
    return seen


@pytest.mark.asyncio
async def test_zero_content_error_stream_raises_turn_stream_failure() -> None:
    err = Error(code=None, message="connection refused", fatal=True)
    llm = _ScriptedLLM([err])
    with pytest.raises(TurnStreamFailure) as exc_info:
        await _drive(llm)
    failure = exc_info.value
    assert failure.error is err
    assert failure.partial_messages == []
    assert failure.rounds_completed == 0


@pytest.mark.asyncio
async def test_partial_content_then_error_raises_with_salvaged_text() -> None:
    err = Error(code="server_error", message="upstream died mid-stream", fatal=True)
    llm = _ScriptedLLM([TextDelta(index=0, text="partial answer"), err])
    with pytest.raises(TurnStreamFailure) as exc_info:
        await _drive(llm)
    failure = exc_info.value
    assert failure.error is err
    assert len(failure.partial_messages) == 1
    assert failure.partial_messages[0].role == "assistant"
    assert failure.rounds_completed == 0


@pytest.mark.asyncio
async def test_genuinely_empty_clean_stream_fails_the_turn() -> None:
    """A Done-terminated, zero-content stream used to end the turn as a
    quiet success (the pre-D-A3-03 behavior this flip replaces): the
    session looked healthy while the model said nothing, and nothing told
    the caller the turn failed. It now fails like any other empty stream,
    so dispatch stamps the ended_detail and the session rests."""
    llm = _ScriptedLLM([Done(stop_reason="stop", raw_reason="stop")])
    with pytest.raises(TurnStreamFailure) as exc_info:
        await _drive(llm)
    failure = exc_info.value
    assert failure.ended_detail_code == "reasoning_only_response"
    assert failure.partial_messages == []
    assert failure.rounds_completed == 0


@pytest.mark.asyncio
async def test_reasoning_only_stream_fails_the_turn() -> None:
    """The D-A3-03 shape: the model streams reasoning but no convertible
    content (the default converter drops ReasoningDelta) and ends with
    Done; the turn must fail with the ended_detail, not succeed empty."""
    llm = _ScriptedLLM([
        ReasoningDelta(text="let me think about it", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ])
    with pytest.raises(TurnStreamFailure) as exc_info:
        await _drive(llm)
    failure = exc_info.value
    assert failure.ended_detail_code == "reasoning_only_response"
    assert failure.partial_messages == []
    assert failure.rounds_completed == 0


@pytest.mark.asyncio
async def test_rounds_completed_counts_only_prior_clean_rounds() -> None:
    """01a070d6 sec 3: emptiness/failure is evaluated per-LLM-call-attempt.
    A later round's failure must report how many EARLIER rounds
    completed cleanly, not conflate the whole turn."""
    from primer.model.chat import ToolCallEnd, ToolCallStart

    err = Error(code=None, message="dropped", fatal=True)
    llm = _ScriptedLLM(
        [
            ToolCallStart(id="tc1", name="loop_tool", index=0),
            ToolCallEnd(id="tc1", arguments={}, index=0),
            Done(stop_reason="tool_use", raw_reason="tool_use"),
        ],
        [err],
    )

    class _OneToolManager(_NoToolManager):
        async def list_tools(self, *, principal=None):
            return [{"name": "loop_tool", "description": "d", "parameters": {}}]

        async def execute(self, call, *, principal=None):
            from primer.model.chat import ToolResultPart
            return ToolResultPart(id=call.id, output="ok", error=False)

    async def _run() -> None:
        async for _ in run_agent_turn(
            agent=_agent(), llm=llm, llm_model=_model(),
            tool_manager=_OneToolManager(),
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
        ):
            pass

    with pytest.raises(TurnStreamFailure) as exc_info:
        await asyncio.wait_for(_run(), timeout=5.0)
    assert exc_info.value.rounds_completed == 1


def test_ended_detail_code_falls_back_when_classifier_left_code_unset() -> None:
    coded = TurnStreamFailure(
        Error(code="llm_connect_error", message="x", fatal=True),
        partial_messages=[], rounds_completed=0,
    )
    assert coded.ended_detail_code == "llm_connect_error"

    uncoded = TurnStreamFailure(
        Error(code=None, message="x", fatal=True),
        partial_messages=[], rounds_completed=0,
    )
    assert uncoded.ended_detail_code == "llm_stream_error"
