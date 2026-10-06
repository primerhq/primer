"""OpenChatLLM against a response that carries SEVERAL tool calls (Phase 3 stage 7a, slice S1-D).

Every other adapter test feeds `_translate_chunk` hand-built single-call chunks. The 7a executor parks a
session on a whole batch, so the thing it depends on is that one assistant response with N calls reaches
the agent loop as N correctly indexed Start/End pairs. This drives the real `OpenChatLLM` (real
`AsyncOpenAI`, real SSE parsing) over the scripted mock with the three wire shapes providers use.
"""

from __future__ import annotations

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import HttpUrl, SecretStr

from primer.llm.openchat import OpenChatLLM
from primer.model.chat import (
    Done,
    Message,
    StreamStart,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.provider import Limits, LLMProvider, LLMProviderType, OpenChatConfig, OpenChatFlavor
from primer.model_profile import ResolvedModel
from tests._support.mock_llm import Rule, ScriptRegistry, ToolEmit, build_app, parallel_tool_batch

BATCH = [
    ToolEmit("lookup", {"q": "alpha", "limit": 3}),
    ToolEmit("fetch", {}),
    ToolEmit("write", {"path": "docs/été.md", "body": {"nested": [1, 2, {"deep": True}]}}, call_id="call_custom"),
]
CHUNKINGS = ["per_call", "batched", "fragmented"]


def _provider() -> LLMProvider:
    return LLMProvider(
        id="mock-openchat",
        provider=LLMProviderType.OPENCHAT,
        models=[ResolvedModel(
            profile_id="p", provider_id="mock-openchat", model_name="scripted:batch",
            context_length=8192, config=ModelProfileConfig(),
        )],
        config=OpenChatConfig(
            url=HttpUrl("http://mock/v1/"), api_key=SecretStr("sk-test"), flavor=OpenChatFlavor.OTHER,
        ),
        limits=Limits(max_concurrency=4),
    )


def _llm_over(registry: ScriptRegistry, monkeypatch) -> OpenChatLLM:
    transport = httpx.ASGITransport(app=build_app(registry))
    real = AsyncOpenAI

    def factory(**kwargs):
        return real(**kwargs, http_client=httpx.AsyncClient(transport=transport))

    monkeypatch.setattr("primer.llm.openchat.AsyncOpenAI", factory)
    return OpenChatLLM(_provider())


async def _events(llm: OpenChatLLM, messages: list[Message]) -> list:
    return [e async for e in llm.stream(model="scripted:batch", messages=messages)]


def _user(text: str = "do all three") -> list[Message]:
    from primer.model.chat import TextPart

    return [Message(role="user", parts=[TextPart(text=text)])]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunking", CHUNKINGS)
async def test_one_response_with_three_calls_becomes_three_indexed_start_end_pairs(chunking, monkeypatch):
    registry = ScriptRegistry()
    registry.register("scripted:batch", parallel_tool_batch(BATCH, chunking=chunking))
    llm = _llm_over(registry, monkeypatch)
    try:
        events = await _events(llm, _user())
    finally:
        await llm.aclose()

    starts = [e for e in events if isinstance(e, ToolCallStart)]
    ends = [e for e in events if isinstance(e, ToolCallEnd)]
    assert [(e.index, e.id, e.name) for e in starts] == [
        (0, "call_0", "lookup"), (1, "call_1", "fetch"), (2, "call_custom", "write"),
    ]
    assert [(e.index, e.id, e.arguments) for e in ends] == [
        (0, "call_0", {"q": "alpha", "limit": 3}),
        (1, "call_1", {}),
        (2, "call_custom", {"path": "docs/été.md", "body": {"nested": [1, 2, {"deep": True}]}}),
    ]
    assert isinstance(events[0], StreamStart)
    assert [type(e) for e in events if isinstance(e, Done)] == [Done]
    done = next(e for e in events if isinstance(e, Done))
    assert done.stop_reason == "tool_use"
    assert not any(isinstance(e, TextDelta) for e in events)

    position = {id(e): i for i, e in enumerate(events)}
    for start, end in zip(starts, ends):
        assert position[id(start)] < position[id(end)] < position[id(done)], "a call must end before Done"
    # deltas only ever name a call that has started, with that call's own id and index
    started: dict[int, str] = {}
    for e in events:
        if isinstance(e, ToolCallStart):
            started[e.index] = e.id
        elif isinstance(e, ToolCallDelta):
            assert started.get(e.index) == e.id


@pytest.mark.asyncio
async def test_a_gateway_that_numbers_every_call_with_index_zero_still_yields_three_calls(monkeypatch):
    """Through the real ``OpenChatLLM`` (real SSE parsing): the three calls are streamed one after the other, all with index 0 and
    their own ids, the argument text split in two with the second half carrying no id. The adapter keyed in-progress calls on the
    index alone, so one call survived with ``{}`` arguments."""
    registry = ScriptRegistry()
    registry.register("scripted:batch", parallel_tool_batch(BATCH, chunking="same_index"))
    llm = _llm_over(registry, monkeypatch)
    try:
        events = await _events(llm, _user())
    finally:
        await llm.aclose()

    starts = [e for e in events if isinstance(e, ToolCallStart)]
    ends = [e for e in events if isinstance(e, ToolCallEnd)]
    assert [(e.id, e.name) for e in starts] == [("call_0", "lookup"), ("call_1", "fetch"), ("call_custom", "write")]
    assert [(e.id, e.arguments) for e in ends] == [
        ("call_0", {"q": "alpha", "limit": 3}),
        ("call_1", {}),
        ("call_custom", {"path": "docs/été.md", "body": {"nested": [1, 2, {"deep": True}]}}),
    ]
    done = next(e for e in events if isinstance(e, Done))
    assert done.stop_reason == "tool_use"
    position = {id(e): i for i, e in enumerate(events)}
    for start, end in zip(starts, ends):
        assert position[id(start)] < position[id(end)] < position[id(done)]


@pytest.mark.asyncio
async def test_fragmented_calls_stream_their_arguments_interleaved_across_indexes(monkeypatch):
    """The shape that breaks an adapter keying state on 'the current call': every header first, then the
    argument text of all three calls in two interleaved passes."""
    registry = ScriptRegistry()
    registry.register("scripted:batch", parallel_tool_batch(BATCH, chunking="fragmented"))
    llm = _llm_over(registry, monkeypatch)
    try:
        events = await _events(llm, _user())
    finally:
        await llm.aclose()

    kinds = [(type(e).__name__, getattr(e, "index", None)) for e in events if isinstance(e, (ToolCallStart, ToolCallDelta))]
    assert kinds[:3] == [("ToolCallStart", 0), ("ToolCallStart", 1), ("ToolCallStart", 2)]
    delta_indexes = [i for name, i in kinds if name == "ToolCallDelta"]
    assert delta_indexes == [0, 1, 2, 0, 1, 2], "two interleaved argument passes over the three calls"


@pytest.mark.asyncio
@pytest.mark.parametrize("chunking", CHUNKINGS)
async def test_the_second_turn_sends_all_three_calls_and_results_back_and_gets_the_final_answer(chunking, monkeypatch):
    registry = ScriptRegistry()
    registry.register("scripted:batch", parallel_tool_batch(BATCH, chunking=chunking, final_text="all three done"))
    llm = _llm_over(registry, monkeypatch)
    try:
        first = await _events(llm, _user())
        calls = [
            ToolCallPart(id=e.id, name=next(s.name for s in first if isinstance(s, ToolCallStart) and s.id == e.id),
                         arguments=e.arguments)
            for e in first if isinstance(e, ToolCallEnd)
        ]
        history = _user() + [
            Message(role="assistant", parts=list(calls)),
            Message(role="tool", parts=[ToolResultPart(id=c.id, output=f"result of {c.name}") for c in reversed(calls)]),
        ]
        second = await _events(llm, history)
    finally:
        await llm.aclose()

    assert "".join(e.text for e in second if isinstance(e, TextDelta)) == "all three done"
    assert not any(isinstance(e, ToolCallStart) for e in second)
    sent = registry.requests[-1]["messages"]
    assistant = next(m for m in sent if m["role"] == "assistant")
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_0", "call_1", "call_custom"]
    tool_rows = [m for m in sent if m["role"] == "tool"]
    assert {m["tool_call_id"]: m["content"] for m in tool_rows} == {
        "call_0": "result of lookup", "call_1": "result of fetch", "call_custom": "result of write",
    }


@pytest.mark.asyncio
async def test_a_single_emit_tool_rule_is_unchanged(monkeypatch):
    registry = ScriptRegistry()
    registry.register("scripted:batch", [Rule(emit_tool="lookup", emit_args={"q": "x"})])
    llm = _llm_over(registry, monkeypatch)
    try:
        events = await _events(llm, _user())
    finally:
        await llm.aclose()

    assert [(e.index, e.id) for e in events if isinstance(e, ToolCallStart)] == [(0, "call_0")]
    assert [e.arguments for e in events if isinstance(e, ToolCallEnd)] == [{"q": "x"}]
