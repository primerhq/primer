"""run_subagent honors the approval gate + yielding tools and pushes an AgentFrame.

These tests drive ``run_subagent`` through a real
:class:`~primer.agent.tool_manager.ToolExecutionManager` (built internally
from a tiny fake toolset provider) so we exercise the actual approval-gate /
yielding-tool wiring rather than a mock of it.

Two scenarios:

* an approval-gated tool: the subagent's turn must raise
  :class:`YieldToWorker` with an ``_approval`` leaf, and a single
  :class:`AgentFrame` must be prepended capturing the in-progress turn.
* a yielding tool (ask_user-style): the tool surface passed to the manager
  must INCLUDE the yielding tool (it is no longer filtered out), so the
  subagent can park on it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.approval import ApprovalResolver
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    StreamStart,
    Tool,
    ToolCallEnd,
    ToolCallResult,
    ToolCallStart,
)
from primer.model.provider import LLMModel
from primer.model.tool_approval import (
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from primer.model.yield_ import Yielded, YieldToWorker


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeLLM:
    """Stub LLM emitting a single scripted tool-calling stream."""

    def __init__(self, *, events: list) -> None:
        self._events = events

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        async def _gen() -> AsyncIterator:
            for ev in self._events:
                yield ev

        return _gen()


class _GatedToolsetProvider:
    """Fake toolset 't1' exposing a single non-yielding tool 'do_it'."""

    def __init__(self) -> None:
        self.last_principal: str | None = None

    async def list_tools(
        self, *, principal: str | None = None
    ) -> AsyncIterator[Tool]:
        yield Tool(
            id="do_it",
            description="does the thing",
            toolset_id="t1",
            args_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
        )

    def is_yielding(self, tool_name: str) -> bool:
        return False

    async def call(
        self, *, tool_name, arguments, principal=None, ctx=None
    ) -> ToolCallResult:  # noqa: ANN001
        return ToolCallResult(output="done", is_error=False)


class _YieldingToolsetProvider:
    """Fake toolset 't1' exposing a yielding tool 'wait' (ask_user-style)."""

    async def list_tools(
        self, *, principal: str | None = None
    ) -> AsyncIterator[Tool]:
        yield Tool(
            id="wait",
            description="waits for the human",
            toolset_id="t1",
            args_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
        )

    def is_yielding(self, tool_name: str) -> bool:
        return tool_name == "wait"

    async def call(
        self, *, tool_name, arguments, principal=None, ctx=None
    ) -> ToolCallResult:  # noqa: ANN001
        # Mirror the real yielding-tool path: raise YieldToWorker keyed on
        # the injected ToolContext's tool_call_id.
        assert ctx is not None, "yielding tool must receive a ToolContext"
        raise YieldToWorker(
            Yielded(
                tool_name="wait",
                event_key=f"ask_user:{ctx.session_id}:{ctx.tool_call_id}",
                resume_metadata={},
            ),
            tool_call_id=ctx.tool_call_id,
        )


class _ProviderRow:
    """Lightweight stand-in: run_subagent only reads ``.models`` off it."""

    def __init__(self, models: list[LLMModel]) -> None:
        self.models = models


class _Store:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def get(self, _id: str) -> Any:
        return self._obj


class _StorageProvider:
    def __init__(self, *, agent: Agent, provider_row: _ProviderRow) -> None:
        self._agent = agent
        self._provider_row = provider_row

    def get_storage(self, cls: type) -> _Store:
        from primer.model.provider import LLMProvider

        if cls is Agent:
            return _Store(self._agent)
        if cls is LLMProvider:
            return _Store(self._provider_row)
        return _Store(None)


class _ProviderRegistry:
    def __init__(self, *, llm: _FakeLLM, toolset: Any) -> None:
        self._llm = llm
        self._toolset = toolset

    async def get_llm(self, _provider_id: str) -> _FakeLLM:
        return self._llm

    async def get_toolset(self, _toolset_id: str) -> Any:
        return self._toolset


class _PoliciesOnlyResolver(ApprovalResolver):
    """Approval resolver returning a fixed policy set without storage."""

    def __init__(self, policies: list[ToolApprovalPolicy]) -> None:
        self._policies = policies
        self._ttl = 60.0
        self._cache = {}

    async def find(self, *, toolset_id, tool_name):  # noqa: ANN001
        for p in self._policies:
            if p.toolset_id == toolset_id and p.tool_name == tool_name:
                return p
        return None


# ===========================================================================
# Helpers
# ===========================================================================


def _agent(*, tools: list[str]) -> Agent:
    return Agent(
        id="agent-sub",
        description="subagent",
        model=AgentModel(provider_id="prov-1", model_name="m1"),
        system_prompt=["you are a subagent"],
        tools=tools,
    )


def _provider_row() -> _ProviderRow:
    return _ProviderRow(models=[LLMModel(name="m1", context_length=128_000)])


def _tool_call_script(scoped_name: str, call_id: str) -> list:
    return [
        StreamStart(model="m1"),
        ToolCallStart(id=call_id, name=scoped_name, index=0),
        ToolCallEnd(id=call_id, arguments={}, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_run_subagent_approval_gate_pushes_agent_frame():
    from primer.agent.invoke import run_subagent

    agent = _agent(tools=["t1__do_it"])
    llm = _FakeLLM(events=_tool_call_script("t1__do_it", "call-1"))
    toolset = _GatedToolsetProvider()
    storage = _StorageProvider(agent=agent, provider_row=_provider_row())
    registry = _ProviderRegistry(llm=llm, toolset=toolset)
    resolver = _PoliciesOnlyResolver(
        [
            ToolApprovalPolicy(
                id="p",
                toolset_id="t1",
                tool_name="do_it",
                approval=RequiredApprovalConfig(),
            ),
        ]
    )

    with pytest.raises(YieldToWorker) as ei:
        await run_subagent(
            agent_id="agent-sub",
            prompt="please do it",
            storage_provider=storage,
            provider_registry=registry,
            principal="user-1",
            approval_resolver=resolver,
            session_id="sess-parent",
            workspace_id="ws-parent",
            chat_id=None,
            invoke_tool_call_id="parent-tcid",
        )

    yld = ei.value
    assert yld.yielded.tool_name == "_approval"
    # Session-scoped event key (not "unknown").
    assert "tool_approval:sess-parent:" in yld.yielded.event_key

    frames = list(getattr(yld, "frames", []) or [])
    assert len(frames) == 1
    frame = frames[0]
    assert frame.kind == "agent"
    assert frame.agent_id == "agent-sub"
    assert frame.tool_call_id == "parent-tcid"
    # The in-progress turn (assistant message that emitted the tool_use)
    # is captured.
    assert frame.llm_messages, "AgentFrame.llm_messages must be non-empty"
    # Resume context carries the inherited identity + tool surface.
    assert frame.context.session_id == "sess-parent"
    assert frame.context.workspace_id == "ws-parent"
    assert frame.context.principal == "user-1"
    assert "t1__do_it" in frame.context.tools


@pytest.mark.asyncio
async def test_run_subagent_keeps_yielding_tools_in_surface():
    from primer.agent.invoke import run_subagent

    agent = _agent(tools=["t1__wait"])
    llm = _FakeLLM(events=_tool_call_script("t1__wait", "call-wait"))
    toolset = _YieldingToolsetProvider()
    storage = _StorageProvider(agent=agent, provider_row=_provider_row())
    registry = _ProviderRegistry(llm=llm, toolset=toolset)

    with pytest.raises(YieldToWorker) as ei:
        await run_subagent(
            agent_id="agent-sub",
            prompt="wait for me",
            storage_provider=storage,
            provider_registry=registry,
            principal="user-1",
            session_id="sess-parent",
            workspace_id="ws-parent",
            invoke_tool_call_id="parent-tcid",
        )

    yld = ei.value
    # The yielding tool was NOT filtered out: it ran and parked.
    assert yld.yielded.tool_name == "wait"
    assert "ask_user:sess-parent:" in yld.yielded.event_key

    frames = list(getattr(yld, "frames", []) or [])
    assert len(frames) == 1
    assert frames[0].kind == "agent"
    assert frames[0].tool_call_id == "parent-tcid"
    assert frames[0].llm_messages
