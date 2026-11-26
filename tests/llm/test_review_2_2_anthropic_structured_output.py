"""Review 2026-11-23 finding 2.2: structured output on the Anthropic adapter must
work with no tools.

Three surfaces used to fail:

* the agent loop sent ``tool_choice="auto"`` even with no tools, and the
  adapter's response_format emulation raised ConfigError for any non-None
  tool_choice, so every agent/graph turn with a response schema died;
* the emulated answer arrived as a ``structured_output`` TOOL CALL, so
  ``judge_structured`` (which keeps only TextDelta) saw no answer: the
  approval judge always failed closed to "required", and memory merge
  confirms silently returned None.

The fix is both sides: the loop sends no tool_choice when there are no
tools, and the adapter streams the emulated answer out as TEXT (the
synthetic block is registered as a text kind, its input JSON becomes
TextDelta, and a ``tool_use`` stop reason with no real tool block maps
to ``stop``). These tests drive the REAL paths: the agent loop with the
real adapter and a fake SDK client (tools empty, response_format set),
the approval judge (``_dispatch_llm_judge``), and the memory merge
confirm (``curate._confirm``).

Round 1 (review 2026-11-23, 2.2 round 1): with REAL tools present the
synthetic tool is OFFERED ALONGSIDE them (tool_choice auto) instead of
raising ConfigError, so a tool-using agent with a response schema works;
and a caller's REAL tool named "structured_output" without
response_format is never swallowed into text (the text path is keyed on
the emulation flag, not the tool name).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

# Reuse the established Anthropic adapter harness (fake SDK client + events).
from tests.llm.test_anthropic import (
    _aiter,
    _make_provider,
    _patched_client,
)

from primer.agent.approval import ApprovalContext, _dispatch_llm_judge
from primer.agent.loop import run_agent_turn
from primer.llm.anthropic import AnthropicLLM
from primer.memory.curate import _confirm
from primer.memory.entry import EntryBody
from primer.memory.store import MemoryEntry
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolResultPart,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.tool_approval import LlmApprovalConfig
from primer.model_profile import ResolvedModel

MODEL = "claude-sonnet-4-5"
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


def _structured_events(payload: str) -> list[NS]:
    """The SDK answer to the emulated structured_output tool call: a
    ``tool_use`` content block named ``structured_output`` whose input
    JSON is the structured answer, delivered in two fragments."""
    mid = max(1, len(payload) // 2)
    return [
        NS(
            type="message_start",
            message=NS(
                id="msg_1",
                model=MODEL,
                usage=NS(input_tokens=5),
            ),
        ),
        NS(
            type="content_block_start",
            index=0,
            content_block=NS(
                type="tool_use", id="tu_1", name="structured_output", input={}
            ),
        ),
        NS(
            type="content_block_delta",
            index=0,
            delta=NS(type="input_json_delta", partial_json=payload[:mid]),
        ),
        NS(
            type="content_block_delta",
            index=0,
            delta=NS(type="input_json_delta", partial_json=payload[mid:]),
        ),
        NS(type="content_block_stop", index=0),
        NS(
            type="message_delta",
            delta=NS(stop_reason="tool_use", stop_sequence=None),
            usage=NS(output_tokens=3),
        ),
        NS(type="message_stop"),
    ]


class _NoToolManager:
    """A tool manager with no tools at all (the failing case: the loop lists
    an empty catalogue and still used to send tool_choice='auto')."""

    workspace_session_scope = (None, None)

    async def list_tools(self, *, principal=None):
        return []


@pytest.mark.asyncio
async def test_agent_turn_with_response_format_and_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real loop + real Anthropic adapter: a turn with a response schema
    and no tools must not raise ConfigError, must deliver the structured
    answer to the caller as text, and must end the turn as a stop (the
    emulated answer is text, not a pending tool call)."""
    client = _patched_client(monkeypatch)
    payload = json.dumps({"value": 7})
    client.messages.create.return_value = _aiter(_structured_events(payload))

    llm = AnthropicLLM(_make_provider())
    messages_out: list[Message] = []
    events: list = []

    async for event in run_agent_turn(
        agent=Agent(id="ag", description="x", model=AgentModel(profile_id="p--m")),
        llm=llm,
        llm_model=ResolvedModel(
            profile_id="test-profile",
            provider_id="test-provider",
            model_name=MODEL,
            context_length=100_000,
            config=ModelProfileConfig(),
        ),
        tool_manager=_NoToolManager(),
        prompt=[Message(role="user", parts=[TextPart(text="answer in the schema")])],
        response_format=SCHEMA,
        messages_out=messages_out,
    ):
        events.append(event)

    # The structured answer reached the caller, as text.
    assert len(messages_out) == 1
    parts = [p for p in messages_out[0].parts if isinstance(p, TextPart)]
    assert len(parts) == 1
    assert json.loads(parts[0].text) == {"value": 7}

    # The turn reads as a stop: the emulated tool_use stop reason must not
    # leave a session waiting on a continuation nothing will queue.
    dones = [e for e in events if isinstance(e, Done)]
    assert dones, "the turn must end with a Done"
    assert dones[0].stop_reason == "stop"
    assert dones[0].raw_reason == "tool_use"

    # The adapter's emulation is what made the call work: it forced the
    # synthetic structured_output tool and its tool_choice on the wire.
    assert client.messages.create.await_count == 1
    request = client.messages.create.await_args.kwargs
    assert request["tools"] == [
        {
            "name": "structured_output",
            "description": "Emit the response in the structured shape defined by input_schema.",
            "input_schema": SCHEMA,
        }
    ]
    assert request["tool_choice"] == {"type": "tool", "name": "structured_output"}


@pytest.mark.asyncio
async def test_approval_llm_judge_with_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approval judge path (approval.py ~536): a verdict must come back
    as a parsed dict. Before the fix the emulated answer arrived as a tool
    call, judge_structured kept only TextDeltas, and the judge failed
    closed to "required" on every gated call."""
    client = _patched_client(monkeypatch)
    verdict_payload = json.dumps({"required": False, "reason": "safe"})
    client.messages.create.return_value = _aiter(_structured_events(verdict_payload))

    llm = AnthropicLLM(_make_provider())
    policy = LlmApprovalConfig(
        provider_id="test-provider", model=MODEL, prompt="judge the call"
    )
    context = ApprovalContext(
        tool_name="run",
        toolset_id="shell",
        arguments={"cmd": "ls"},
        agent_id="ag",
        session_id="s",
        chat_id=None,
        requested_at=datetime.now(timezone.utc),
    )
    registry = NS(_sp=None, get_llm=AsyncMock(return_value=llm))

    out = await _dispatch_llm_judge(
        policy=policy, context=context, provider_registry=registry
    )
    assert out == {"required": False, "reason": "safe"}


@pytest.mark.asyncio
async def test_memory_merge_confirm_with_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory merge path (curate.py ~140): a usable merge answer must
    come back as a MergeOutput. Before the fix the ValueError from the
    empty text path was swallowed and every Anthropic merge was a no-op."""
    client = _patched_client(monkeypatch)
    merge_payload = json.dumps(
        {"merged": True, "description": "merged", "steps": ["step one"], "pitfalls": []}
    )
    client.messages.create.return_value = _aiter(_structured_events(merge_payload))

    llm = AnthropicLLM(_make_provider())
    model = NS(model_name=MODEL)
    older = MemoryEntry(
        name="deploy",
        status="active",
        description="how to deploy",
        body=EntryBody(steps=[("s1", "old step")], pitfalls=[], example=[]),
    )
    newer = MemoryEntry(
        name="deploy",
        status="active",
        description="how to deploy",
        body=EntryBody(steps=[("s1", "new step")], pitfalls=[], example=[]),
    )

    out = await _confirm(llm=llm, model=model, older=older, newer=newer)
    assert out is not None
    assert out.merged is True
    assert out.steps == ["step one"]


# ===========================================================================
# Round 1 (review 2026-11-23, 2.2 round 1)
# ===========================================================================


class _OneToolManager:
    """A tool manager with exactly ONE real tool (the round-1 case: tools
    present alongside a response schema)."""

    workspace_session_scope = (None, None)

    def __init__(self) -> None:
        self.executed: list = []

    async def list_tools(self, *, principal=None):
        return [
            Tool(id="lookup", description="d", toolset_id="ws", args_schema={"type": "object"})
        ]

    def is_notifying(self, name: str) -> bool:
        del name
        return False

    async def execute(self, call, *, principal=None):
        self.executed.append(call)
        return ToolResultPart(id=call.id, output="found: 7", error=False)


def _real_tool_use_events(payload: str, name: str = "lookup", block_id: str = "tu_lookup") -> list[NS]:
    """A real (fake-SDK) Anthropic tool_use block for the REAL tool
    ``lookup`` (not the synthetic one), plus a completed message."""
    return [
        NS(type="message_start", message=NS(id="msg_1", model=MODEL, usage=NS(input_tokens=5))),
        NS(
            type="content_block_start",
            index=0,
            content_block=NS(type="tool_use", id=block_id, name=name, input={}),
        ),
        NS(
            type="content_block_delta",
            index=0,
            delta=NS(type="input_json_delta", partial_json=payload),
        ),
        NS(type="content_block_stop", index=0),
        NS(
            type="message_delta",
            delta=NS(stop_reason="tool_use", stop_sequence=None),
            usage=NS(output_tokens=3),
        ),
        NS(type="message_stop"),
    ]


@pytest.mark.asyncio
async def test_agent_turn_with_response_format_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1: WITH real tools the emulation must NOT raise ConfigError. The
    synthetic structured_output tool is OFFERED ALONGSIDE the caller's tools
    with tool_choice auto, so the real tool runs and the final structured
    answer still arrives as text (the round-0 fix refused the combination)."""
    client = _patched_client(monkeypatch)
    structured_payload = json.dumps({"value": 7})
    client.messages.create.side_effect = [
        _aiter(_real_tool_use_events(json.dumps({"q": "lookup"}))),
        _aiter(_structured_events(structured_payload)),
    ]

    llm = AnthropicLLM(_make_provider())
    tm = _OneToolManager()
    messages_out: list[Message] = []
    events: list = []

    async for event in run_agent_turn(
        agent=Agent(id="ag", description="x", model=AgentModel(profile_id="p--m")),
        llm=llm,
        llm_model=ResolvedModel(
            profile_id="test-profile",
            provider_id="test-provider",
            model_name=MODEL,
            context_length=100_000,
            config=ModelProfileConfig(),
        ),
        tool_manager=tm,
        prompt=[Message(role="user", parts=[TextPart(text="answer in the schema")])],
        response_format=SCHEMA,
        messages_out=messages_out,
    ):
        events.append(event)

    # The REAL tool ran (it was not replaced by the synthetic one).
    assert len(tm.executed) == 1
    assert tm.executed[0].name == "lookup"

    # The final answer is the structured payload, delivered as text.
    final = [m for m in messages_out if m.role == "assistant"][-1]
    parts = [p for p in final.parts if isinstance(p, TextPart)]
    assert json.loads(parts[-1].text) == {"value": 7}

    dones = [e for e in events if isinstance(e, Done)]
    assert dones and dones[-1].stop_reason == "stop"

    # The wire request OFFERED the synthetic tool alongside the real one with
    # tool_choice auto (round 0 raised ConfigError for this combination).
    assert client.messages.create.await_count == 2
    first = client.messages.create.await_args_list[0].kwargs
    assert [t["name"] for t in first["tools"]] == ["lookup", "structured_output"]
    assert first["tools"][1]["input_schema"] == SCHEMA
    assert first["tool_choice"] == {"type": "auto"}


@pytest.mark.asyncio
async def test_real_structured_output_tool_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1: a caller's REAL tool named ``structured_output`` WITHOUT
    response_format must stream as an ordinary tool call, not be swallowed
    into text. The text path is keyed on the emulation flag, not the name."""
    client = _patched_client(monkeypatch)
    client.messages.create.return_value = _aiter(
        _real_tool_use_events(
            json.dumps({"value": 7}),
            name="structured_output",
            block_id="tu_real",
        )
    )

    llm = AnthropicLLM(_make_provider())
    events: list = []
    async for event in llm.stream(
        model=MODEL,
        messages=[Message(role="user", parts=[TextPart(text="use the tool")])],
        tools=[
            Tool(
                id="structured_output",
                description="d",
                toolset_id="ws",
                args_schema={"type": "object"},
            )
        ],
    ):
        events.append(event)

    ends = [e for e in events if isinstance(e, ToolCallEnd)]
    assert len(ends) == 1
    assert ends[0].arguments == {"value": 7}
    # It was a tool call, not swallowed into text.
    assert not any(isinstance(e, TextDelta) for e in events)
    # No emulation was active, so the synthetic tool was not offered.
    request = client.messages.create.await_args.kwargs
    assert [t["name"] for t in request["tools"]] == ["structured_output"]
    assert "tool_choice" not in request or request.get("tool_choice") is None


@pytest.mark.asyncio
async def test_real_structured_output_tool_with_response_format_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 finding 2: response_format plus a REAL tool named ``structured_output``
    would send two same-named tools (a 400 from Anthropic). Raise a clear
    ConfigError naming the clash, before any request."""
    from primer.model.except_ import ConfigError

    client = _patched_client(monkeypatch)
    llm = AnthropicLLM(_make_provider())
    with pytest.raises(ConfigError, match="structured_output"):
        async for _ in llm.stream(
            model=MODEL,
            messages=[Message(role="user", parts=[TextPart(text="go")])],
            response_format=SCHEMA,
            tools=[
                Tool(
                    id="structured_output",
                    description="d",
                    toolset_id="ws",
                    args_schema={"type": "object"},
                )
            ],
        ):
            pass
    client.messages.create.assert_not_called()
