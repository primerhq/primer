"""Review 2026-11-23 finding 2.5: a tool call whose streamed argument JSON was
truncated or malformed must NOT run.

The adapters (Anthropic's content_block_stop, primer.llm._openai_common
``parse_tool_arguments`` used by the OpenAI adapters) settle a broken
argument stream on ``{}`` so the stream survives; the loop used to run the
call on that fallback anyway. The loop now re-derives the validity from the
raw ``ToolCallDelta`` fragments it already buffers: a call whose fragments
do not parse as a JSON object is answered with an error result naming the
parse failure, and only the remaining calls dispatch.

Covered here: a truncated JSON fragment and a valid-JSON-but-not-an-object
one, through the loop (the mechanism, with a duck-typed LLM) and end to end
with the REAL Anthropic and OpenResponses adapters and fake SDK clients.
The benign cases -- well-formed arguments, a double-encoded string holding
a JSON object (which the adapters decode twice and accept), and a
no-argument call (a single BLANK fragment, Anthropic's documented shape) --
must still run.

Round 1 (review 2026-11-23, 2.5 round 1): a round whose calls are ALL
malformed continues to the next model round instead of ending the turn, the
refusal reaches the model's next prompt as the tool_result of the refused
call in the SAME tool message as the dispatched results (one message, call
order, or Anthropic 400s the unbalanced history), and the adapters settle a
double-encoded argument stream on its real arguments.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace as NS

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
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

from tests.llm.test_anthropic import _aiter, _make_provider as _make_anthropic_provider, _patched_client as _patched_anthropic
from tests.llm.test_openresponses import _make_provider as _make_openresponses_provider, _patched_client as _patched_openresponses

from primer.llm.anthropic import AnthropicLLM
from primer.llm.openresponses import OpenResponsesLLM

_REFUSAL_MARKER = "could not be parsed as a JSON object"
_TOOL_NAME = "write_file"


class _RecordingToolManager:
    """A duck-typed tool manager that records every call it is asked to run."""

    workspace_session_scope = (None, None)

    def __init__(self) -> None:
        self.executed: list[ToolCallPart] = []

    async def list_tools(self, *, principal=None):
        return [Tool(id=_TOOL_NAME, description="d", toolset_id="ws", args_schema={"type": "object"})]

    def is_notifying(self, name: str) -> bool:
        del name
        return False

    async def execute(self, call, *, principal=None):
        self.executed.append(call)
        return ToolResultPart(id=call.id, output="ok", error=False)


def _agent() -> Agent:
    return Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"))


def _llm_model(model_name: str) -> ResolvedModel:
    return ResolvedModel(
        profile_id="test-profile",
        provider_id="test-provider",
        model_name=model_name,
        context_length=100_000,
        config=ModelProfileConfig(),
    )


def _tool_results(messages_out: list[Message]) -> list[ToolResultPart]:
    for msg in reversed(messages_out):
        if msg.role == "tool":
            return [p for p in msg.parts if isinstance(p, ToolResultPart)]
    return []


def _assistant_text(messages_out: list[Message]) -> str:
    """The text of the LAST assistant message (the turn's final answer)."""
    for msg in reversed(messages_out):
        if msg.role == "assistant":
            return "".join(p.text for p in msg.parts if isinstance(p, TextPart))
    return ""


async def _drive(llm, tm: _RecordingToolManager):
    messages_out: list[Message] = []

    async for _ in run_agent_turn(
        agent=_agent(),
        llm=llm,
        llm_model=_llm_model("m"),
        tool_manager=tm,
        prompt=[Message(role="user", parts=[TextPart(text="write a file")])],
        messages_out=messages_out,
    ):
        pass
    return messages_out


class _ArgsLLM:
    """Streams one tool call: a single ToolCallDelta of ``fragment``, then a
    ToolCallEnd with the adapter's ``{}`` fallback (what both real adapters
    emit for a broken argument stream). A second round (the turn feeding back
    a tool result) answers with plain text and stops."""

    def __init__(self, fragment: str) -> None:
        self._fragment = fragment
        self._round = 0

    def stream(self, *, model, messages, **kwargs):
        self._round += 1
        if self._round > 1:
            async def _done() -> AsyncIterator[StreamEvent]:
                yield TextDelta(text="done", index=0)
                yield Done(stop_reason="stop", raw_reason="end_turn")

            return _done()

        async def _gen() -> AsyncIterator[StreamEvent]:
            yield ToolCallStart(id="tc1", name=_TOOL_NAME, index=0)
            yield ToolCallDelta(id="tc1", arguments_delta=self._fragment, index=0)
            yield ToolCallEnd(id="tc1", arguments={}, index=0)
            yield Done(stop_reason="max_tokens", raw_reason="max_tokens")

        return _gen()

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_loop_refuses_truncated_arguments() -> None:
    """A JSON object cut off mid-string (the max_tokens scenario): the tool
    does not run, and the model is told the arguments failed to parse."""
    tm = _RecordingToolManager()
    llm = _ArgsLLM('{"path": "a.tx')
    messages_out = await _drive(llm, tm)

    assert tm.executed == [], "a truncated call must not run"
    results = _tool_results(messages_out)
    assert len(results) == 1
    assert results[0].id == "tc1"
    assert results[0].error is True
    assert _REFUSAL_MARKER in results[0].output


@pytest.mark.asyncio
async def test_loop_refuses_non_object_arguments() -> None:
    """Valid JSON that is not an object (the adapter settles it on ``{}``):
    the call does not run either."""
    tm = _RecordingToolManager()
    llm = _ArgsLLM("[1, 2]")
    messages_out = await _drive(llm, tm)

    assert tm.executed == []
    results = _tool_results(messages_out)
    assert len(results) == 1
    assert results[0].error is True
    assert _REFUSAL_MARKER in results[0].output


@pytest.mark.asyncio
async def test_loop_runs_well_formed_arguments() -> None:
    """Benign: complete arguments still run exactly as before."""
    tm = _RecordingToolManager()
    llm = _ArgsLLM('{"path": "a.txt", "content": "x"}')
    messages_out = await _drive(llm, tm)

    assert len(tm.executed) == 1
    assert tm.executed[0].id == "tc1"
    results = _tool_results(messages_out)
    assert len(results) == 1
    assert results[0].error is False
    assert results[0].output == "ok"


@pytest.mark.asyncio
async def test_loop_runs_double_encoded_arguments() -> None:
    """Benign: a string holding a JSON object. The adapters decode it twice
    and accept it; the loop must agree and run the call."""
    tm = _RecordingToolManager()
    llm = _ArgsLLM(json.dumps('{"path": "a.txt"}'))
    messages_out = await _drive(llm, tm)

    assert len(tm.executed) == 1
    results = _tool_results(messages_out)
    assert len(results) == 1
    assert results[0].error is False
    assert results[0].output == "ok"


def _anthropic_tool_events(payload: str) -> list[NS]:
    """A real (fake-SDK) Anthropic stream whose tool_use input JSON is
    ``payload``: fragments stream, then the stop settles the args on ``{}``
    when they do not parse."""
    return [
        NS(type="message_start", message=NS(id="msg_1", model="m", usage=NS(input_tokens=5))),
        NS(
            type="content_block_start",
            index=0,
            content_block=NS(type="tool_use", id="tu_1", name=_TOOL_NAME, input={}),
        ),
        NS(
            type="content_block_delta",
            index=0,
            delta=NS(type="input_json_delta", partial_json=payload),
        ),
        NS(type="content_block_stop", index=0),
        NS(
            type="message_delta",
            delta=NS(stop_reason="max_tokens", stop_sequence=None),
            usage=NS(output_tokens=3),
        ),
        NS(type="message_stop"),
    ]


def _openresponses_tool_events(payload: str) -> list[NS]:
    """A real (fake-SDK) OpenResponses stream: the delta fragments and the
    ``done`` event both carry the same (broken) argument JSON."""
    return [
        NS(
            type="response.output_item.added",
            item=NS(type="function_call", id="item_1", call_id="call_1", name=_TOOL_NAME),
        ),
        NS(type="response.function_call_arguments.delta", item_id="item_1", delta=payload),
        NS(type="response.function_call_arguments.done", item_id="item_1", arguments=payload),
        NS(type="response.completed", response=NS(usage=None)),
    ]


def _anthropic_text_events(text: str = "ok, retrying") -> list[NS]:
    """A real (fake-SDK) Anthropic text stream: the round the model answers
    after a tool result (a resend, or a plain text answer)."""
    return [
        NS(type="message_start", message=NS(id="msg_2", model="m", usage=NS(input_tokens=8))),
        NS(type="content_block_start", index=0, content_block=NS(type="text", text="")),
        NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text=text)),
        NS(type="content_block_stop", index=0),
        NS(
            type="message_delta",
            delta=NS(stop_reason="end_turn", stop_sequence=None),
            usage=NS(output_tokens=4),
        ),
        NS(type="message_stop"),
    ]


def _openresponses_text_events(text: str = "ok, retrying") -> list[NS]:
    """The OpenResponses equivalent of :func:`_anthropic_text_events`."""
    return [
        NS(type="response.output_item.added", item=NS(type="message", id="m_2")),
        NS(type="response.content_part.added", item_id="m_2", content_index=0),
        NS(type="response.output_text.delta", item_id="m_2", content_index=0, delta=text),
        NS(type="response.completed", response=NS(usage=None)),
    ]


class _ResendLLM:
    """Three rounds: round 1 a single MALFORMED call (the whole round is
    refused), round 2 the valid resend, round 3 a text answer. The turn must
    run all three: an all-malformed round ends the turn on the old loop
    (review 2026-11-23, 2.5 round 1)."""

    def __init__(self) -> None:
        self._round = 0

    def stream(self, *, model, messages, **kwargs):
        self._round += 1
        round_no = self._round

        async def _gen() -> AsyncIterator[StreamEvent]:
            if round_no == 1:
                yield ToolCallStart(id="tc1", name=_TOOL_NAME, index=0)
                yield ToolCallDelta(id="tc1", arguments_delta='{"path": "a.tx', index=0)
                yield ToolCallEnd(id="tc1", arguments={}, index=0)
            elif round_no == 2:
                yield ToolCallStart(id="tc2", name=_TOOL_NAME, index=0)
                yield ToolCallDelta(
                    id="tc2", arguments_delta='{"path": "a.txt"}', index=0
                )
                yield ToolCallEnd(id="tc2", arguments={"path": "a.txt"}, index=0)
            else:
                yield TextDelta(text="done", index=0)
            yield Done(
                stop_reason="stop" if round_no == 3 else "max_tokens",
                raw_reason="end_turn" if round_no == 3 else "max_tokens",
            )

        return _gen()

    async def aclose(self) -> None:
        pass


async def _drive_adapter(llm, tm: _RecordingToolManager, model_name: str):
    messages_out: list[Message] = []

    async for _ in run_agent_turn(
        agent=_agent(),
        llm=llm,
        llm_model=_llm_model(model_name),
        tool_manager=tm,
        prompt=[Message(role="user", parts=[TextPart(text="write a file")])],
        messages_out=messages_out,
    ):
        pass
    return messages_out


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ['{"path": "a.tx', "[1, 2]"])
async def test_anthropic_broken_arguments_not_run(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """End to end: real Anthropic adapter + fake SDK. The adapter settles the
    broken stream on ``{}`` (its stream-survival contract is unchanged); the
    loop must still refuse to run the call, answer it in the round's tool
    message, and continue to a second model round (round 1)."""
    client = _patched_anthropic(monkeypatch)
    client.messages.create.side_effect = [
        _aiter(_anthropic_tool_events(payload)),
        _aiter(_anthropic_text_events()),
    ]
    tm = _RecordingToolManager()
    messages_out = await _drive_adapter(AnthropicLLM(_make_anthropic_provider()), tm, "claude-sonnet-4-5")

    assert tm.executed == [], f"the tool ran on a {payload!r} argument stream"
    results = _tool_results(messages_out)
    assert len(results) == 1
    assert results[0].error is True
    assert _REFUSAL_MARKER in results[0].output
    # The turn continued: a second assistant (text) message followed the refusal.
    assert _assistant_text(messages_out) == "ok, retrying"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ['{"path": "a.tx', "[1, 2]"])
async def test_openresponses_broken_arguments_not_run(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """End to end: real OpenResponses adapter + fake SDK, same contract."""

    async def _agen(events):
        for event in events:
            yield event

    client = _patched_openresponses(monkeypatch)
    client.responses.create.side_effect = [
        _agen(_openresponses_tool_events(payload)),
        _agen(_openresponses_text_events()),
    ]
    tm = _RecordingToolManager()
    messages_out = await _drive_adapter(
        OpenResponsesLLM(_make_openresponses_provider()), tm, "gpt-4o-mini"
    )

    assert tm.executed == [], f"the tool ran on a {payload!r} argument stream"
    results = _tool_results(messages_out)
    assert len(results) == 1
    assert results[0].error is True
    assert _REFUSAL_MARKER in results[0].output
    # The turn continued: a second assistant (text) message followed the refusal.
    assert _assistant_text(messages_out) == "ok, retrying"


# ===========================================================================
# Round 1 (review 2026-11-23, 2.5 round 1)
# ===========================================================================


def _anthropic_two_call_events() -> list[NS]:
    """Round 1 with TWO tool_use blocks: index 0 well-formed, index 1
    truncated mid-JSON. The well-formed one must run, the truncated one is
    refused, and both are answered in the next prompt's one tool message."""
    return [
        NS(type="message_start", message=NS(id="msg_1", model="m", usage=NS(input_tokens=5))),
        NS(
            type="content_block_start",
            index=0,
            content_block=NS(type="tool_use", id="tu_valid", name=_TOOL_NAME, input={}),
        ),
        NS(
            type="content_block_delta",
            index=0,
            delta=NS(type="input_json_delta", partial_json='{"path": "a.txt"}'),
        ),
        NS(type="content_block_stop", index=0),
        NS(
            type="content_block_start",
            index=1,
            content_block=NS(type="tool_use", id="tu_bad", name=_TOOL_NAME, input={}),
        ),
        NS(
            type="content_block_delta",
            index=1,
            delta=NS(type="input_json_delta", partial_json='{"path": "b.tx'),
        ),
        NS(type="content_block_stop", index=1),
        NS(
            type="message_delta",
            delta=NS(stop_reason="tool_use", stop_sequence=None),
            usage=NS(output_tokens=9),
        ),
        NS(type="message_stop"),
    ]


@pytest.mark.asyncio
async def test_loop_continues_after_all_malformed_round() -> None:
    """IMPORTANT 4: a round whose calls are ALL malformed does not end the
    turn: the refusal is the round's answer, and the model gets a next round
    to resend a complete call (the old loop returned after the refusal)."""
    tm = _RecordingToolManager()
    llm = _ResendLLM()
    messages_out = await _drive(llm, tm)

    assert llm._round == 3, "the all-malformed round ended the turn"
    assert [c.id for c in tm.executed] == ["tc2"]
    tool_msgs = [m for m in messages_out if m.role == "tool"]
    assert len(tool_msgs) == 2
    first = [p for p in tool_msgs[0].parts if isinstance(p, ToolResultPart)]
    second = [p for p in tool_msgs[1].parts if isinstance(p, ToolResultPart)]
    assert first[0].id == "tc1" and first[0].error is True
    assert _REFUSAL_MARKER in first[0].output
    assert second[0].id == "tc2" and second[0].error is False
    assert second[0].output == "ok"
    assert _assistant_text(messages_out) == "done"


@pytest.mark.asyncio
async def test_anthropic_no_argument_tool_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL 3: a no-argument call streams a single BLANK
    ``partial_json`` fragment (Anthropic's documented shape). The loop must
    treat that as valid empty arguments and RUN the call, not refuse it."""
    client = _patched_anthropic(monkeypatch)
    client.messages.create.side_effect = [
        _aiter(_anthropic_tool_events("")),
        _aiter(_anthropic_text_events()),
    ]
    tm = _RecordingToolManager()
    await _drive_adapter(AnthropicLLM(_make_anthropic_provider()), tm, "claude-sonnet-4-5")

    assert len(tm.executed) == 1, "a no-argument call was refused"
    assert tm.executed[0].arguments == {}


@pytest.mark.asyncio
async def test_anthropic_double_encoded_arguments_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minor: a double-encoded argument stream (a JSON string holding a JSON
    object) is decoded twice by the shared settle helper, so the call runs on
    its REAL arguments rather than the ``{}`` fallback."""
    client = _patched_anthropic(monkeypatch)
    client.messages.create.side_effect = [
        _aiter(_anthropic_tool_events(json.dumps('{"path": "a.txt"}'))),
        _aiter(_anthropic_text_events()),
    ]
    tm = _RecordingToolManager()
    await _drive_adapter(AnthropicLLM(_make_anthropic_provider()), tm, "claude-sonnet-4-5")

    assert len(tm.executed) == 1
    assert tm.executed[0].arguments == {"path": "a.txt"}


@pytest.mark.asyncio
async def test_anthropic_refusal_reaches_prompt_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL 2: the refusal reaches the model's NEXT prompt as the
    tool_result of the refused call, in the SAME tool message as the
    dispatched results (one message, call order, is_error set) -- or
    Anthropic 400s the unbalanced history."""
    client = _patched_anthropic(monkeypatch)
    client.messages.create.side_effect = [
        _aiter(_anthropic_two_call_events()),
        _aiter(_anthropic_text_events()),
    ]
    tm = _RecordingToolManager()
    await _drive_adapter(AnthropicLLM(_make_anthropic_provider()), tm, "claude-sonnet-4-5")

    assert [c.id for c in tm.executed] == ["tu_valid"]
    assert client.messages.create.await_count == 2
    second = client.messages.create.await_args_list[1].kwargs["messages"]
    assistant = [m for m in second if m["role"] == "assistant"][-1]
    assert [
        b["id"] for b in assistant["content"] if b.get("type") == "tool_use"
    ] == ["tu_valid", "tu_bad"]
    tr_msgs = [
        m
        for m in second
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and m["content"]
        and all(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert len(tr_msgs) == 1, "the round's answers must be ONE tool message"
    blocks = {b["tool_use_id"]: b for b in tr_msgs[0]["content"]}
    assert list(blocks) == ["tu_valid", "tu_bad"]
    assert blocks["tu_valid"]["content"] == "ok"
    assert blocks["tu_valid"]["is_error"] is False
    assert _REFUSAL_MARKER in blocks["tu_bad"]["content"]
    assert blocks["tu_bad"]["is_error"] is True
