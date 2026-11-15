"""Translating the claude CLI's stream-json stdout into primer stream events."""

from __future__ import annotations

import json
from pathlib import Path

from primer.common.context_overflow import is_context_overflow_error
from primer.llm.claude_code.translate import StreamTranslator
from primer.model.chat import (
    Done,
    Error,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from primer.model.except_ import AuthenticationError, RateLimitError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code"

# The spike's MCP tool was mcp__p__get_weather; the scoped primer id behind it.
UNALIAS = {"get_weather": "weather__get_weather"}.get


def _run(name: str, unalias=UNALIAS) -> tuple[list, StreamTranslator]:
    """Feed the fixture line by line, the way the provider feeds it: stop once
    the translator has produced a terminal event (the provider kills the CLI
    on message_stop, so nothing after a Done is ever fed)."""
    t = StreamTranslator(unalias=unalias)
    events = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        events.extend(t.feed(json.loads(line)))
        if t.done:
            break
    return events, t


def test_text_and_usage():
    events, t = _run("text_with_tool.ndjson")
    assert any(isinstance(e, TextDelta) for e in events)
    assert any(isinstance(e, Usage) for e in events)
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"


def test_parallel_tool_round():
    events, t = _run("parallel_tools.ndjson")
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    ends = [e for e in events if isinstance(e, ToolCallEnd)]
    assert len(starts) == len(ends) == 3
    assert {e.name for e in starts} == {"weather__get_weather"}  # unalias applied
    assert {frozenset(e.arguments.items()) for e in ends} == {
        frozenset({"city": "Paris"}.items()),
        frozenset({"city": "Tokyo"}.items()),
        frozenset({"city": "Lima"}.items()),
    }
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "tool_use" and events[-1].raw_reason == "tool_use"
    assert t.kill_now


def test_thinking_becomes_reasoning():
    events, _t = _run("thinking.ndjson")
    assert any(isinstance(e, ReasoningDelta) for e in events)


def test_incomplete_tool_json_emits_no_end():
    t = StreamTranslator(unalias=UNALIAS)
    events = []
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_01", "name": "mcp__p__get_weather", "input": {}}}})
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"city": "Pa'}}})
    # The block stops (as a max_tokens cutoff sends) but the JSON never closed:
    # the accumulated fragments do not parse, so no ToolCallEnd may be emitted.
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 0}})
    events += t.feed({"type": "stream_event", "event": {
        "type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {
            "input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 0}}})
    events += t.feed({"type": "stream_event", "event": {"type": "message_stop"}})
    assert isinstance(events[0], ToolCallStart)
    assert any(isinstance(e, ToolCallDelta) for e in events)
    assert not any(isinstance(e, ToolCallEnd) for e in events)
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "max_tokens"
    assert not t.kill_now


def test_unknown_event_types_produce_nothing():
    t = StreamTranslator(unalias=UNALIAS)
    for line_type in ("system", "assistant", "user", "tool_progress", "control_response"):
        assert t.feed({"type": line_type, "foo": "bar"}) == []
    assert not t.done


def test_interrupt_fixture_emits_terminal_error():
    # The fixture holds the interrupted step's stdout (plus the resumed one);
    # a single step's feed stops once the failing result's Error is produced.
    t = StreamTranslator(unalias=UNALIAS)
    events = []
    for line in (FIXTURES / "interrupt.ndjson").read_text(encoding="utf-8").splitlines():
        events.extend(t.feed(json.loads(line)))
        if events and isinstance(events[-1], Error):
            break
    assert any(isinstance(e, TextDelta) for e in events)
    assert any(isinstance(e, Done) for e in events)  # the interrupted message still got a Done
    assert isinstance(events[-1], Error) and events[-1].fatal
    assert events[-1].message


def test_usage_maps_cached_input():
    events, _t = _run("parallel_tools.ndjson")
    usages = [e for e in events if isinstance(e, Usage)]
    assert usages and all(u.cumulative is False for u in usages)
    assert any(u.cached_input_tokens for u in usages)  # from cache_read_input_tokens


def test_result_errors_map():
    t = StreamTranslator(unalias=UNALIAS)
    assert isinstance(
        t.error_for_result({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}),
        AuthenticationError,
    )


def test_plan_limit_maps_to_rate_limit():
    t = StreamTranslator(unalias=UNALIAS)
    t.feed({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
    assert isinstance(
        t.error_for_result({"type": "result", "is_error": True, "result": "..."}),
        RateLimitError,
    )


def test_error_for_result_none_when_not_failing():
    t = StreamTranslator(unalias=UNALIAS)
    assert t.error_for_result({"type": "result", "is_error": False, "result": "ok"}) is None
    assert t.error_for_result({"type": "assistant"}) is None


def test_error_for_result_generic_is_none():
    t = StreamTranslator(unalias=UNALIAS)
    assert t.error_for_result({"type": "result", "is_error": True, "result": "boom", "subtype": "error_during_execution"}) is None


def test_feed_failing_result_emits_fatal_error():
    t = StreamTranslator(unalias=UNALIAS)
    events = t.feed({"type": "result", "is_error": True, "result": "boom", "subtype": "error_during_execution"})
    assert len(events) == 1
    assert isinstance(events[0], Error) and events[0].fatal and events[0].message == "boom"
    assert t.done


def test_failing_result_overflow_keeps_the_cli_text_as_bad_request():
    # Spec 6.6 v1.5: a too-long prompt is bad_request (not
    # claude_code_result_error), keeping the CLI text, so primer's
    # context-overflow handling recognises it on both channels.
    t = StreamTranslator(unalias=UNALIAS)
    [overflow] = t.feed({"type": "result", "is_error": True,
                         "result": "Prompt is too long: 210000 tokens > 200000 maximum"})
    assert overflow.fatal and overflow.code == "bad_request"
    assert overflow.message == "Prompt is too long: 210000 tokens > 200000 maximum"
    assert is_context_overflow_error(overflow)
    # Anything else keeps the generic code.
    [other] = StreamTranslator(unalias=UNALIAS).feed(
        {"type": "result", "is_error": True, "result": "model exploded"})
    assert other.code == "claude_code_result_error"
    assert not is_context_overflow_error(other)


# --- Fix round 1: whole-prompt token count + plan-limit text fallback ---

def test_usage_sums_cached_prompt_tokens():
    # The API's input_tokens excludes cache reads and cache creation; the
    # reported input is the whole prompt (mirrors primer/llm/anthropic.py).
    # The fixture's first message_start: input_tokens 2, cache_read 755,
    # cache_creation 344 -> 1101.
    events, _t = _run("parallel_tools.ndjson")
    usages = [e for e in events if isinstance(e, Usage)]
    first = usages[0]
    assert first.input_tokens == 2 + 755 + 344
    assert first.cached_input_tokens == 755


def test_plan_limit_text_maps_to_rate_limit():
    # No rate_limit_event seen: a plan-limit phrasing in the result text still
    # maps to a RateLimitError.
    t = StreamTranslator(unalias=UNALIAS)
    for text in ("You have hit your usage limit.", "Error: rate limit exceeded, please retry later"):
        assert isinstance(
            t.error_for_result({"type": "result", "is_error": True, "result": text}),
            RateLimitError,
        ), text


def test_unrelated_failing_text_is_not_rate_limit():
    # The fallback must not over-match: unrelated failing text maps to None.
    t = StreamTranslator(unalias=UNALIAS)
    assert t.error_for_result({"type": "result", "is_error": True, "result": "Something exploded"}) is None


# --- Fix round 2: the CLI's internal StructuredOutput tool (spec 6.7 v1.4) ---

def test_structured_output_is_held_not_emitted_at_block_stop():
    # The fixture is the real-CLI shape: init lists tools ["StructuredOutput"],
    # the model calls it, the CLI answers it itself, the message ends with
    # stop_reason tool_use and the result line carries structured_output.
    # Spec 6.7 v1.5: the CLI validates the input and retries on mismatch, so the
    # ANSWER is the result line's structured_output -- the translator emits NO
    # text at all; the structured-only message's Done is still produced (stop,
    # raw tool_use, no kill) for the adapter to hold until the result line.
    t = StreamTranslator(unalias=UNALIAS)
    events = []
    for line in (FIXTURES / "structured_output.ndjson").read_text(encoding="utf-8").splitlines():
        events.extend(t.feed(json.loads(line)))
    # It is NOT a primer tool call: no ToolCall* events, and no answer text either.
    assert not any(isinstance(e, (ToolCallStart, ToolCallDelta, ToolCallEnd)) for e in events)
    assert not any(isinstance(e, TextDelta) for e in events), "the answer comes from the result line, not the block"
    # The message's Done is produced (stop maps to stop, raw keeps tool_use) and
    # the process is NOT killed for it; the adapter holds the Done and reads on.
    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason == "stop" and events[-1].raw_reason == "tool_use"
    assert not t.kill_now


def test_structured_output_with_truncated_input_emits_nothing():
    # A cut-off StructuredOutput input (max_tokens mid-JSON) is not a usable
    # answer: no text at all.
    t = StreamTranslator(unalias=UNALIAS)
    events = []
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "struct_1", "name": "StructuredOutput", "input": {}}}})
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"pri'}}})
    events += t.feed({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    events += t.feed({"type": "stream_event", "event": {
        "type": "message_delta", "delta": {"stop_reason": "tool_use"}}})
    events += t.feed({"type": "stream_event", "event": {"type": "message_stop"}})
    assert not any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"


def test_structured_output_retry_holds_every_done():
    # Spec 6.7 v1.5: the first attempt's input does not match the schema; the CLI
    # makes the model retry. Every Done is held (for the adapter to wait for the
    # result line), no kill, no text; the adapter takes the result line's
    # structured_output as the answer.
    t = StreamTranslator(unalias=UNALIAS)
    events = []
    for line in (FIXTURES / "structured_output_retry.ndjson").read_text(encoding="utf-8").splitlines():
        events.extend(t.feed(json.loads(line)))
    assert not any(isinstance(e, (ToolCallStart, ToolCallDelta, ToolCallEnd)) for e in events)
    assert not any(isinstance(e, TextDelta) for e in events)
    dones = [e for e in events if isinstance(e, Done)]
    assert len(dones) == 2  # one per attempt; the adapter holds them
    assert all(d.stop_reason == "stop" and d.raw_reason == "tool_use" for d in dones)
    assert not t.kill_now


def test_mcp_tool_still_kills_when_structured_output_also_present():
    # A message carrying BOTH a primer tool call and the StructuredOutput call:
    # the mcp__p__ tool keeps its normal tool_use events and kill behaviour.
    t = StreamTranslator(unalias=UNALIAS)
    events = []
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_01", "name": "mcp__p__get_weather", "input": {}}}})
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"city": "Paris"}'}}})
    events += t.feed({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "id": "struct_1", "name": "StructuredOutput", "input": {}}}})
    events += t.feed({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"prime": true}'}}})
    events += t.feed({"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}})
    events += t.feed({"type": "stream_event", "event": {
        "type": "message_delta", "delta": {"stop_reason": "tool_use"}}})
    events += t.feed({"type": "stream_event", "event": {"type": "message_stop"}})
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    assert [e.name for e in starts] == ["weather__get_weather"]
    assert any(isinstance(e, ToolCallEnd) for e in events)  # the primer tool call is intact
    assert not any(isinstance(e, TextDelta) for e in events)  # the structured input is never text
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "tool_use"
    assert t.kill_now
