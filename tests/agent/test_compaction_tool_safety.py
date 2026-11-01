"""The tool-enabled compaction summariser: no fabricated summary, no yielding tool (AG-M5, AG-M22).

AG-M5: a summariser round with no text and no executed tool call must fail like the text-only summariser
(``ServerError("compaction produced empty summary text")``) instead of folding the head into the
"delegated the detail to tools" placeholder. A round that did execute tools still gets the placeholder.

AG-M22: a yielding tool acts before it parks (an external tool writes a pending ``ExternalToolCall`` row, an MCP
task starts on the server); compaction runs outside the park machinery, so the summariser is not offered those
tools and a call to one is answered with an error result without being dispatched.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import mcp.types as mcp_types
import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.invoke import _SubagentSession
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Error,
    Message,
    ReasoningDelta,
    StreamEvent,
    StreamStart,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.except_ import ServerError
from primer.model.external_tool import ExternalToolDef
from primer.model.model_profile import ModelProfileConfig
from primer.model.principal import PrincipalRef
from primer.model_profile import ResolvedModel
from tests.toolset.test_mcp_tasks import _FakeSession, _FakeStdioProvider

PLACEHOLDER = "delegated the detail to tools"


class _ScriptedLLM:
    def __init__(self, rounds: list[list[StreamEvent]]) -> None:
        self._rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": list(messages), **kwargs})
        script = self._rounds.pop(0) if self._rounds else [Done(stop_reason="stop", raw_reason="stop")]

        async def _gen() -> AsyncIterator[StreamEvent]:
            for ev in script:
                yield ev

        return _gen()


class _Tools:
    """A tool manager listing ``tools`` and recording every dispatched call."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools = list(tools or [])
        self.executed: list[str] = []

    async def list_tools(self, *, principal: str | None = None) -> list[Tool]:
        return list(self._tools)

    async def execute(self, call: ToolCallPart, *, principal: str | None = None) -> ToolResultPart:
        self.executed.append(call.name)
        return ToolResultPart(id=call.id, output="ok", error=False)


def _agent() -> Agent:
    return Agent(id="a1", description="d", model=AgentModel(profile_id="p--m"))


def _model() -> ResolvedModel:
    return ResolvedModel(profile_id="p", provider_id="prov", model_name="m1", context_length=32_000, config=ModelProfileConfig())


def _history(n: int = 12) -> list[Message]:
    out: list[Message] = []
    for i in range(n):
        out.append(Message(role="user", parts=[TextPart(text=f"question {i} " + "x" * 4000)]))
        out.append(Message(role="assistant", parts=[TextPart(text=f"answer {i} " + "y" * 4000)]))
    return out


def _tool(name: str, *, yields: bool = False) -> Tool:
    return Tool(id=name, description="d", toolset_id=name.split("__")[0], args_schema={"type": "object"}, yields=yields)


def _tool_round(cid: str, name: str) -> list[StreamEvent]:
    return [
        StreamStart(model="m1"),
        ToolCallStart(id=cid, name=name, index=0),
        ToolCallEnd(id=cid, arguments={}, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


def _text_round(text: str) -> list[StreamEvent]:
    return [StreamStart(model="m1"), TextDelta(text=text, index=0), Done(stop_reason="stop", raw_reason="stop")]


EMPTY = [StreamStart(model="m1"), ReasoningDelta(text="thinking...", index=0), Done(stop_reason="max_tokens", raw_reason="length")]


def _summary(out) -> str:
    return "".join(p.text for p in out.summary_message.parts if isinstance(p, TextPart))


async def _compact(llm, tm, **kw):
    return await CompactionStrategy().force_compact(
        agent=_agent(), llm=llm, model=_model(), history=_history(), tool_manager=tm, **kw,
    )


# --- characterization: pass before and after ---------------------------------------------------------------------


async def test_text_only_empty_stream_raises_empty_summary() -> None:
    with pytest.raises(ServerError, match="compaction produced empty summary text"):
        await CompactionStrategy().force_compact(agent=_agent(), llm=_ScriptedLLM([EMPTY]), model=_model(), history=_history())


async def test_text_only_fatal_error_raises_llm_failed() -> None:
    bad = [StreamStart(model="m1"), Error(code="network_error", message="down", fatal=True)]
    with pytest.raises(ServerError, match="compaction LLM failed: down"):
        await CompactionStrategy().force_compact(agent=_agent(), llm=_ScriptedLLM([bad]), model=_model(), history=_history())


async def test_text_only_summary_text_unchanged() -> None:
    out = await CompactionStrategy().force_compact(
        agent=_agent(), llm=_ScriptedLLM([_text_round("the plain summary")]), model=_model(), history=_history(),
    )
    assert _summary(out).endswith("\n\nthe plain summary")


async def test_tool_only_round_then_empty_round_gets_the_placeholder() -> None:
    tm = _Tools([_tool("workspace__write")])
    out = await _compact(_ScriptedLLM([_tool_round("c1", "workspace__write"), EMPTY]), tm)
    assert tm.executed == ["workspace__write"]
    assert PLACEHOLDER in _summary(out)
    assert out.head_messages_replaced > 0


async def test_tool_turn_cap_after_an_executed_round_gets_the_placeholder() -> None:
    tm = _Tools([_tool("workspace__write")])
    rounds = [_tool_round(f"c{i}", "workspace__write") for i in range(5)]
    out = await _compact(_ScriptedLLM(rounds), tm, max_tool_turns=2)
    assert tm.executed == ["workspace__write"]
    assert PLACEHOLDER in _summary(out)


async def test_summariser_offers_every_non_yielding_tool() -> None:
    plain = [_tool("workspace__write"), _tool("system__list_agents"), _tool("mcp1__search")]
    llm = _ScriptedLLM([_text_round("s")])
    await _compact(llm, _Tools([*plain, _tool("system__ask_user", yields=True)]))
    offered = [t.id for t in llm.calls[0]["tools"]]
    assert [t.id for t in plain] == [i for i in offered if i in {t.id for t in plain}]


async def test_a_call_to_an_unlisted_tool_is_still_dispatched() -> None:
    """A name the catalogue does not hold goes to the tool manager as before (it answers 'unknown tool')."""
    tm = _Tools([])
    out = await _compact(_ScriptedLLM([_tool_round("c1", "workspace__write"), _text_round("done")]), tm)
    assert tm.executed == ["workspace__write"]
    assert _summary(out).endswith("done")


# --- red: AG-M5 ------------------------------------------------------------------------------------------------


async def test_empty_first_round_with_tools_raises_and_keeps_the_head() -> None:
    with pytest.raises(ServerError, match="compaction produced empty summary text"):
        await _compact(_ScriptedLLM([EMPTY]), _Tools([_tool("workspace__write")]))


async def test_rounds_that_only_call_withheld_tools_do_not_get_the_placeholder() -> None:
    tm = _Tools([_tool("system__ask_user", yields=True)])
    with pytest.raises(ServerError, match="compaction produced empty summary text"):
        await _compact(_ScriptedLLM([_tool_round("c1", "system__ask_user"), EMPTY]), tm)
    assert tm.executed == []


# --- red: AG-M22 -----------------------------------------------------------------------------------------------


def _manager(**kw) -> ToolExecutionManager:
    return ToolExecutionManager(
        workspace_session=_SubagentSession(session_id="sess-1", workspace_id="ws-1", agent_id="a1"),
        initiated_by=PrincipalRef(type="user", id="u1", display="u", role="user", source="local"),
        **kw,
    )


class _Rows:
    def __init__(self) -> None:
        self.created: list[Any] = []

    async def create(self, row):
        self.created.append(row)
        return row


async def test_external_tool_call_during_compaction_leaves_no_row() -> None:
    rows = _Rows()
    tm = _manager(
        external_tools=[ExternalToolDef(name="ask_client", description="d", schema={"type": "object"})],
        external_call_storage=rows,
    )
    llm = _ScriptedLLM([_tool_round("c1", "external__ask_client"), _text_round("the summary")])
    strategy = CompactionStrategy()
    text, _cut = await strategy._summarise_with_tools(
        strategy._summary_request("summarise", _history(2)),
        llm=llm, model=_model(), tool_manager=tm, event_sink=None, max_tool_turns=None, principal=None,
    )
    assert text == "the summary"
    assert rows.created == []
    assert "external__ask_client" not in [t.id for t in llm.calls[0]["tools"]]
    answer = llm.calls[1]["messages"][-1]
    assert answer.role == "tool"
    [result] = answer.parts
    assert result.id == "c1" and result.error is True
    assert "external__ask_client" in result.output


class _RecordingSession(_FakeSession):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.started: list[str] = []

    async def call_tool(self, name, arguments=None, **kw):
        self.started.append(name)
        return await super().call_tool(name, arguments, **kw)


async def test_mcp_task_tool_is_not_offered_nor_started_during_compaction() -> None:
    task_tool = mcp_types.Tool(
        name="long_running", description="d", inputSchema={"type": "object"},
        execution=mcp_types.ToolExecution(taskSupport="required"),
    )
    plain = mcp_types.Tool(name="lookup", description="d", inputSchema={"type": "object"})
    session = _RecordingSession(tools=[task_tool, plain])
    tm = _manager(toolset_providers={"srv": _FakeStdioProvider(session, toolset_id="srv")})
    llm = _ScriptedLLM([_tool_round("c1", "srv__long_running"), _text_round("the summary")])
    strategy = CompactionStrategy()
    text, _cut = await strategy._summarise_with_tools(
        strategy._summary_request("summarise", _history(2)),
        llm=llm, model=_model(), tool_manager=tm, event_sink=None, max_tool_turns=None, principal=None,
    )
    assert text == "the summary"
    assert [t.id for t in llm.calls[0]["tools"]] == ["srv__lookup"]
    assert session.started == []
    [result] = llm.calls[1]["messages"][-1].parts
    assert result.error is True
