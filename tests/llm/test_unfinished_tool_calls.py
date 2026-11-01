"""A tool call that never finished streaming is never run (Part 2 T51, AG-M17).

``output_to_message`` filled ``{}`` for a call with a start and no end, and the agent loop ran it with those arguments: a
mid-stream failover kept the failed member's half call, and a Chat Completions stream cut before its ``finish_reason``
ended quietly with no terminal event at all.
"""

from __future__ import annotations

import pytest

from primer.llm.aggregated import AggregatedLLM
from primer.model.chat import (
    Done,
    Error as ChatError,
    Message,
    StreamStart,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    output_to_message,
)
from primer.model.model_profile import FailoverPoint, ModelProfile, ModelProfileConfig
from primer.model_profile import ResolvedModel
from tests.llm.test_adapter_characterization import _ADAPTERS, _OK, _chat_chunk, _collect, _provider, _route, _sse

MSGS = [Message(role="user", parts=[TextPart(text="hi")])]


class _Scripted:
    def __init__(self, events) -> None:
        self._events = events

    def profile_extended(self, config, *, model=None):
        return {}

    async def stream(self, **kwargs):
        for event in self._events:
            yield event


class TestOutputToMessage:
    def test_a_call_with_no_end_is_dropped(self) -> None:
        msg = output_to_message([
            StreamStart(model="m"),
            TextDelta(text="Let me check", index=0),
            ToolCallStart(id="call_a", name="delete_file", index=1),
            ToolCallDelta(id="call_a", arguments_delta='{"pa', index=1),
            ChatError(fatal=True, code="network_error", message="dropped"),
        ])
        assert msg.parts == [TextPart(text="Let me check")]

    def test_a_stream_holding_only_an_unfinished_call_has_no_content(self) -> None:
        with pytest.raises(ValueError):
            output_to_message([StreamStart(model="m"), ToolCallStart(id="call_a", name="delete_file", index=0)])

    def test_finished_calls_are_kept_in_order(self) -> None:
        msg = output_to_message([
            ToolCallStart(id="a", name="x", index=0),
            ToolCallEnd(id="a", arguments={"n": 1}, index=0),
            ToolCallStart(id="b", name="y", index=1),
            ToolCallStart(id="c", name="z", index=2),
            ToolCallEnd(id="c", arguments={}, index=2),
        ])
        assert msg.parts == [ToolCallPart(id="a", name="x", arguments={"n": 1}), ToolCallPart(id="c", name="z", arguments={})]


async def test_a_mid_stream_failover_leaves_no_phantom_call() -> None:
    members = {
        "ma": _Scripted([
            StreamStart(request_id="a", model="a"),
            TextDelta(text="Let me check", index=0),
            ToolCallStart(id="toolu_A", name="delete_file", index=1),
            ToolCallDelta(id="toolu_A", arguments_delta='{"pa', index=1),
            ChatError(fatal=True, code="server_error", message="overloaded"),
        ]),
        "mb": _Scripted([
            StreamStart(request_id="b", model="b"),
            ToolCallStart(id="toolu_B", name="delete_file", index=1),
            ToolCallEnd(id="toolu_B", arguments={"path": "tmp/x"}, index=1),
            Done(stop_reason="tool_use", raw_reason="tool_use"),
        ]),
    }

    async def resolve(member_id):
        return members[member_id], ResolvedModel(
            profile_id=member_id, provider_id="p", model_name="m", context_length=8192, config=ModelProfileConfig(),
        )

    profile = ModelProfile(id="agg", description="d", kind="aggregated", members=["ma", "mb"],
                           failover_point=FailoverPoint.MID_STREAM)
    events = [e async for e in AggregatedLLM(profile, resolve_member=resolve).stream(model="x", messages=MSGS)]
    calls = [p for p in output_to_message(events).parts if isinstance(p, ToolCallPart)]
    assert calls == [ToolCallPart(id="toolu_B", name="delete_file", arguments={"path": "tmp/x"})]


_CUT = _sse([
    _chat_chunk({"role": "assistant", "content": "Let me"}),
    _chat_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                 "function": {"name": "delete_file", "arguments": '{"path": "/da'}}]}),
])


@pytest.mark.parametrize("kind", ["openchat", "openrouter"])
@pytest.mark.parametrize("ending", [b"data: [DONE]\n\n", b""], ids=["done-marker", "eof"])
async def test_a_stream_cut_before_its_finish_reason_ends_with_a_network_error(monkeypatch, kind, ending) -> None:
    _route(monkeypatch, kind, [(200, _CUT + ending, _OK)])
    events = await _collect(_ADAPTERS[kind](_provider(kind)), kind)
    assert [type(e).__name__ for e in events] == ["StreamStart", "TextDelta", "ToolCallStart", "ToolCallDelta", "Error"]
    assert (events[-1].fatal, events[-1].code) == (True, "network_error")
    assert output_to_message(events).parts == [TextPart(text="Let me")]


@pytest.mark.parametrize("kind", ["openchat", "openrouter"])
async def test_an_empty_stream_still_yields_nothing(monkeypatch, kind) -> None:
    _route(monkeypatch, kind, [(200, b"data: [DONE]\n\n", _OK)])
    assert await _collect(_ADAPTERS[kind](_provider(kind)), kind) == []
