"""Tool arguments that parse to something other than an object (Part 2 T51, AG-M18).

The OpenAI-family translators handed a double-encoded JSON string, ``null`` or a list straight to ``ToolCallEnd``; pydantic
refused it and the adapter reported a call that succeeded as a failed stream. Anthropic already coerced to ``{}``; now the
OpenAI family does too, decoding one more level when the value is a string holding a JSON object.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from primer.llm._openai_compat import _end_tool_call, _ToolCallInProgress
from primer.llm.openresponses import _StreamState, _translate_event
from primer.model.chat import ToolCallEnd
from tests.llm.test_adapter_characterization import _ADAPTERS, _OK, _chat_chunk, _collect, _provider, _route, _sse

CASES = [
    (json.dumps(json.dumps({"path": "a"})), {"path": "a"}),
    ("null", {}),
    ("[1]", {}),
    ('"just text"', {}),
    (json.dumps(json.dumps([1])), {}),
    ("7", {}),
    ('{"path": "a"}', {"path": "a"}),
    ("not json", {}),
    ("", {}),
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_chat_completions(raw, expected) -> None:
    end = _end_tool_call(_ToolCallInProgress(call_id="c", name="n", arguments_buffer=raw))
    assert end.arguments == expected


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_responses(raw, expected) -> None:
    state = _StreamState()
    _translate_event(NS(type="response.output_item.added",
                        item=NS(type="function_call", id="i1", call_id="c1", name="n")), state)
    out = _translate_event(NS(type="response.function_call_arguments.done", item_id="i1", arguments=raw), state)
    assert [e.arguments for e in out if isinstance(e, ToolCallEnd)] == [expected]


@pytest.mark.parametrize("kind", ["openchat", "openrouter"])
async def test_the_adapter_reports_the_call_not_a_failed_stream(monkeypatch, kind) -> None:
    _route(monkeypatch, kind, [(200, _sse([
        _chat_chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps(json.dumps({"path": "a"}))}}]}),
        _chat_chunk({}, "tool_calls"),
    ]) + b"data: [DONE]\n\n", _OK)])
    events = await _collect(_ADAPTERS[kind](_provider(kind)), kind)
    assert [type(e).__name__ for e in events] == ["StreamStart", "ToolCallStart", "ToolCallDelta", "ToolCallEnd", "Done"]
    assert events[3].arguments == {"path": "a"}
