"""judge_structured: one structured verdict from any adapter.

approval.py has called this since the LLM approval type shipped, and no
adapter implemented it: every llm-judged gate failed closed with
"llm gate unavailable" (verified live). The method now lives on the LLM
ABC itself, built from the one thing every adapter already has - a
stream() that accepts a JSON-schema response_format - so a second
adapter can never ship without it.
"""
from __future__ import annotations

import pytest

from primer.int.llm import LLM
from primer.model.chat import Done, Error, TextDelta

_SCHEMA = {
    "type": "object",
    "properties": {"required": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["required"],
    "additionalProperties": False,
}


class _Scripted(LLM):
    """Yields a fixed event list; records the call for assertions."""

    def __init__(self, events):
        self._events = events
        self.calls: list[dict] = []

    async def count_tokens(self, *, model, messages):  # pragma: no cover
        return 0

    async def stream(self, *, model, messages, temperature=None, top_p=None,
                     max_output_tokens=None, stop=None, response_format=None,
                     tools=None, tool_choice=None, extended=None):
        self.calls.append({
            "model": model, "messages": messages,
            "response_format": response_format, "temperature": temperature,
        })
        for e in self._events:
            yield e


def _deltas(text: str, n: int = 3):
    """Split text into n TextDelta chunks to mimic real streaming."""
    step = max(1, len(text) // n)
    return [TextDelta(text=text[i:i + step], index=0)
            for i in range(0, len(text), step)]


async def _judge(llm):
    return await llm.judge_structured(
        model="m", system_prompt="judge it",
        user_message="{}", response_schema=_SCHEMA,
    )


async def test_clean_json_verdict_parses():
    llm = _Scripted(_deltas('{"required": true, "reason": "risky"}'))
    verdict = await _judge(llm)
    assert verdict == {"required": True, "reason": "risky"}
    call = llm.calls[0]
    assert call["response_format"] == _SCHEMA, (
        "the schema must reach the provider so structured mode is enforced"
    )
    assert call["temperature"] == 0.0, "verdicts must not be sampled hot"


async def test_fenced_or_chatty_output_still_yields_the_object():
    """Reasoning models wrap JSON in prose or fences; extract, not reject."""
    text = 'Thinking about it...\n```json\n{"required": false}\n```\nDone.'
    llm = _Scripted(_deltas(text, n=5))
    assert (await _judge(llm)) == {"required": False}


async def test_fatal_stream_error_raises():
    llm = _Scripted([Error(message="model_starting", fatal=True)])
    with pytest.raises(RuntimeError, match="model_starting"):
        await _judge(llm)


async def test_garbage_output_raises_rather_than_guessing():
    llm = _Scripted(_deltas("I cannot decide, sorry."))
    with pytest.raises(ValueError):
        await _judge(llm)


async def test_non_text_events_are_ignored():
    events = [Done(stop_reason="stop", raw_reason="stop"), *_deltas('{"required": true}')]
    llm = _Scripted(events)
    assert (await _judge(llm))["required"] is True


# ---- the whole approval path, previously dead -----------------------------


class _Registry:
    def __init__(self, llm):
        self._llm = llm

    async def get_llm(self, provider_id):
        return self._llm


async def test_llm_gate_verdict_flows_through_evaluate_approval_gate():
    from datetime import datetime, timezone

    from primer.agent.approval import ApprovalContext, evaluate_approval_gate
    from primer.model.tool_approval import LlmApprovalConfig, ToolApprovalPolicy

    policy = ToolApprovalPolicy(
        id="p-llm", toolset_id="workspace", tool_name="write",
        approval=LlmApprovalConfig(provider_id="x", model="m", prompt="judge"),
    )
    context = ApprovalContext(
        tool_name="write", toolset_id="workspace", arguments={"path": "a"},
        agent_id="operator", session_id="s", chat_id=None,
        requested_at=datetime.now(timezone.utc),
    )
    llm = _Scripted(_deltas('{"required": false, "reason": "harmless"}'))
    verdict = await evaluate_approval_gate(
        policy=policy, context=context, provider_registry=_Registry(llm),
    )
    assert verdict.required is False
    assert verdict.reason == "harmless"


async def test_llm_gate_still_fails_closed_when_the_judge_breaks():
    from datetime import datetime, timezone

    from primer.agent.approval import ApprovalContext, evaluate_approval_gate
    from primer.model.tool_approval import LlmApprovalConfig, ToolApprovalPolicy

    policy = ToolApprovalPolicy(
        id="p-llm", toolset_id="workspace", tool_name="write",
        approval=LlmApprovalConfig(provider_id="x", model="m", prompt="judge"),
    )
    context = ApprovalContext(
        tool_name="write", toolset_id="workspace", arguments={"path": "a"},
        agent_id="operator", session_id="s", chat_id=None,
        requested_at=datetime.now(timezone.utc),
    )
    llm = _Scripted([Error(message="down", fatal=True)])
    verdict = await evaluate_approval_gate(
        policy=policy, context=context, provider_registry=_Registry(llm),
    )
    assert verdict.required is True
    assert "unavailable" in verdict.reason
