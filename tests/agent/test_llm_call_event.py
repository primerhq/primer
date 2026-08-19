"""S7 section 5: one llm_call event per model call, emitted before Done.

Ordering matters: the record for the final call must land inside the
turn's seq window, and the window closes on the DONE record.
"""

from __future__ import annotations

import pytest

from primer.agent.loop import run_agent_turn
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.agent import Agent
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallStart,
    Usage,
    _LlmCall,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel


class _ScriptedLLM:
    def __init__(self, rounds):
        self._rounds = list(rounds)

    def stream(self, **_kwargs):
        events = self._rounds.pop(0)

        async def _gen():
            for ev in events:
                yield ev

        return _gen()


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="prof-1",
        provider_id="prov-1",
        model_name="m-1",
        context_length=1000,
        config=ModelProfileConfig(),
    )


async def _drain(llm) -> list:
    out = []
    async for ev in run_agent_turn(
        agent=Agent(id="ag-1", description="d", model={"profile_id": "prof-1"}),
        llm=llm,
        llm_model=_model(),
        tool_manager=ToolExecutionManager(toolset_providers={}, tools=[]),
        prompt=[Message(role="user", parts=[TextPart(text="hi")])],
    ):
        out.append(ev)
    return out


def _llm_calls(events) -> list:
    return [
        e.extended for e in events
        if isinstance(e, ExtendedEvent) and isinstance(e.extended, _LlmCall)
    ]


async def test_one_event_per_call_with_the_profile_payload():
    events = await _drain(_ScriptedLLM([[
        TextDelta(text="hello", index=0),
        Usage(input_tokens=11, output_tokens=7, cumulative=False),
        Done(stop_reason="stop", raw_reason="stop"),
    ]]))
    calls = _llm_calls(events)
    assert len(calls) == 1
    call = calls[0]
    assert call.profile_id == "prof-1"
    assert call.provider_id == "prov-1"
    assert call.model == "m-1"
    assert call.input_tokens == 11
    assert call.output_tokens == 7
    assert call.status == "ok"
    assert call.duration_ms >= 0


async def test_the_event_precedes_its_round_done():
    events = await _drain(_ScriptedLLM([[
        Usage(input_tokens=1, output_tokens=1, cumulative=False),
        Done(stop_reason="stop", raw_reason="stop"),
    ]]))
    kinds = [
        "llm_call" if isinstance(e, ExtendedEvent) and isinstance(e.extended, _LlmCall)
        else e.type
        for e in events
    ]
    assert kinds.index("llm_call") < kinds.index("done")
    assert kinds[-1] == "done"


async def test_one_event_per_tool_round():
    # The tool is not registered on the empty manager, so the manager
    # raises UnsupportedContentError and _dispatch_tool_calls converts it
    # to an error ToolResultPart (primer/agent/loop.py:209-211). The loop
    # still re-arms, which is exactly the second model call under test.
    events = await _drain(_ScriptedLLM([
        [
            ToolCallStart(id="c1", name="noop", index=0),
            ToolCallEnd(id="c1", arguments={}, index=0),
            Done(stop_reason="tool_use", raw_reason="tool_use"),
        ],
        [
            TextDelta(text="done", index=0),
            Done(stop_reason="stop", raw_reason="stop"),
        ],
    ]))
    assert len(_llm_calls(events)) == 2


async def test_missing_usage_yields_null_token_counts():
    events = await _drain(_ScriptedLLM([[
        TextDelta(text="hi", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]]))
    call = _llm_calls(events)[0]
    assert call.input_tokens is None
    assert call.output_tokens is None
