"""Real adapter translation, with isolated SDK streams and no provider calls."""
import json
from types import SimpleNamespace as NS

import pytest

from primer.llm.anthropic import AnthropicLLM
from primer.memory import reflection_input as ri
from primer.model.chat import ExtendedEvent, RawReasoningDelta
from primer.model.memory import ReflectionOutput
from tests.llm.test_anthropic import _aiter, _make_provider, _ok_events, _patched_client
from tests.memory.test_reflection_input import MODEL, Stream, judge


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("limited", [False, True])
async def test_anthropic_synthetic_schema_result_is_data_not_execution(monkeypatch, summary, limited):
    client = _patched_client(monkeypatch)
    llm = AnthropicLLM(_make_provider())
    answer = {"facts": []} if summary else {"run_succeeded": True, "deltas": []}
    events = _ok_events()
    events[1:4] = [
        NS(type="content_block_start", index=0, content_block=NS(type="tool_use", id="result", name="structured_output")),
        NS(type="content_block_delta", index=0, delta=NS(type="input_json_delta", partial_json=json.dumps(answer))),
        NS(type="content_block_stop", index=0),
    ]
    events[-2].delta.stop_reason = "max_tokens" if limited else "tool_use"
    client.messages.create.return_value = _aiter(events)
    schema = (ri.EvidenceSummary if summary else ReflectionOutput).model_json_schema()
    work = ri.bounded_judge(llm, MODEL, system="instructions", user="evidence", schema=schema,
                            budget=ri.Budget.for_model(MODEL.context_length, summary=summary))
    if limited:
        with pytest.raises(ri.InputRefused, match="compaction_output_limit"):
            await work
    else:
        assert await work == answer
    request = client.messages.create.call_args.kwargs
    assert request["tools"] == [{"name": "structured_output",
                                  "description": "Emit the response in the structured shape defined by input_schema.",
                                  "input_schema": schema}]
    assert request["tool_choice"] == {"type": "tool", "name": "structured_output"}
    assert client.messages.create.call_count == 1


@pytest.mark.parametrize("repeated", [False, True])
async def test_nested_raw_reasoning_counts_toward_byte_ceiling(repeated):
    text = "x" * (ri.MAX_OUTPUT_BYTES // 2 + 1 if repeated else ri.MAX_OUTPUT_BYTES + 1)
    event = ExtendedEvent(extended=RawReasoningDelta(text=text, index=0))
    llm = Stream([event, event] if repeated else [event])
    with pytest.raises(ri.InputRefused, match="compaction_output_limit"):
        await judge(llm)
    assert llm.closed
