"""resume_subagent rebuilds + continues a parked subagent turn.

Task 4.2 of the unified nested-yield resume feature extracts two reusable
pieces out of ``run_subagent``:

* ``build_subagent_toolmanager`` - builds the
  :class:`~primer.agent.tool_manager.ToolExecutionManager` over an
  :class:`~primer.worker.frames.AgentResumeContext`'s tool surface, with the
  approval gate + provider_registry + ``_SubagentSession`` shim. Shared by
  ``run_subagent`` (DRY) and ``resume_subagent``.
* ``resume_subagent`` - rehydrates the parked turn's ``llm_messages``, appends
  the now-completed child's tool result, and re-runs ``run_agent_turn`` so the
  LLM continues past the tool call. Returns the final assistant text, or
  re-parks (prepending a FRESH :class:`AgentFrame`) if the continuation yields.

These tests drive both helpers through a real ``ToolExecutionManager`` built
from a tiny fake toolset provider, mirroring ``test_run_subagent_yield``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamStart,
    TextDelta,
    Tool,
    ToolCallEnd,
    ToolCallResult,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.frames import AgentResumeContext


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeLLM:
    """Stub LLM emitting a single scripted stream (messages ignored)."""

    def __init__(self, *, events: list) -> None:
        self._events = events

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        async def _gen() -> AsyncIterator:
            for ev in self._events:
                yield ev

        return _gen()


class _PlainToolsetProvider:
    """Fake toolset 't1' exposing a single non-yielding tool 'do_it'."""

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


def _context(*, tools: list[str]) -> AgentResumeContext:
    return AgentResumeContext(
        session_id="sess-parent",
        workspace_id="ws-parent",
        chat_id=None,
        principal="user-1",
        tools=tools,
    )


def _final_text_script(text: str) -> list:
    return [
        StreamStart(model="m1"),
        TextDelta(text=text, index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]


def _tool_call_script(scoped_name: str, call_id: str) -> list:
    return [
        StreamStart(model="m1"),
        ToolCallStart(id=call_id, name=scoped_name, index=0),
        ToolCallEnd(id=call_id, arguments={}, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


def _parked_llm_messages(scoped_name: str, call_id: str) -> list[dict]:
    """A one-message mid-flight history: the assistant turn that emitted the
    tool_use that parked (serialised to list[dict], as on an AgentFrame)."""
    from primer.model.chat import ToolCallPart

    assistant = Message(
        role="assistant",
        parts=[ToolCallPart(id=call_id, name=scoped_name, arguments={})],
    )
    return [assistant.model_dump(mode="json")]


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_subagent_completes_returns_text():
    from primer.agent.invoke import resume_subagent

    agent = _agent(tools=["t1__do_it"])
    # Continuation: after the injected tool result, the LLM produces final text.
    llm = _FakeLLM(events=_final_text_script("the answer is 42"))
    toolset = _PlainToolsetProvider()
    storage = _StorageProvider(agent=agent, provider_row=_provider_row())
    registry = _ProviderRegistry(llm=llm, toolset=toolset)

    out = await resume_subagent(
        agent_id="agent-sub",
        context=_context(tools=["t1__do_it"]),
        llm_messages=_parked_llm_messages("t1__do_it", "call-1"),
        child_result=ToolResultPart(id="call-1", output="42", error=False),
        depth=1,
        storage_provider=storage,
        provider_registry=registry,
        approval_resolver=None,
        invoke_tool_call_id="parent-tcid",
    )

    assert isinstance(out, str)
    assert "the answer is 42" in out


@pytest.mark.asyncio
async def test_resume_subagent_reyields_pushes_agent_frame():
    from primer.agent.invoke import resume_subagent

    agent = _agent(tools=["t1__wait"])
    # Continuation: the LLM immediately calls a yielding tool -> re-park.
    llm = _FakeLLM(events=_tool_call_script("t1__wait", "call-wait"))
    toolset = _YieldingToolsetProvider()
    storage = _StorageProvider(agent=agent, provider_row=_provider_row())
    registry = _ProviderRegistry(llm=llm, toolset=toolset)

    with pytest.raises(YieldToWorker) as ei:
        await resume_subagent(
            agent_id="agent-sub",
            context=_context(tools=["t1__wait"]),
            llm_messages=_parked_llm_messages("t1__wait", "call-1"),
            child_result=ToolResultPart(id="call-1", output="ok", error=False),
            depth=1,
            storage_provider=storage,
            provider_registry=registry,
            approval_resolver=None,
            invoke_tool_call_id="parent-tcid",
        )

    yld = ei.value
    assert yld.yielded.tool_name == "wait"
    frames = list(getattr(yld, "frames", []) or [])
    assert len(frames) == 1
    frame = frames[0]
    assert frame.kind == "agent"
    assert frame.tool_call_id == "parent-tcid"
    # The NEW continuation delta (the assistant message that emitted the
    # re-yielding tool_use) is captured, not the original parked history.
    assert frame.llm_messages, "AgentFrame.llm_messages must be non-empty"


@pytest.mark.asyncio
async def test_build_subagent_toolmanager_includes_tools():
    from primer.agent.invoke import build_subagent_toolmanager

    toolset = _PlainToolsetProvider()
    registry = _ProviderRegistry(llm=_FakeLLM(events=[]), toolset=toolset)
    storage = _StorageProvider(
        agent=_agent(tools=["t1__do_it"]), provider_row=_provider_row()
    )

    manager = await build_subagent_toolmanager(
        _context(tools=["t1__do_it"]),
        storage_provider=storage,
        provider_registry=registry,
        approval_resolver=None,
    )

    tools = await manager.list_tools(principal="user-1")
    ids = {t.id for t in tools}
    assert "t1__do_it" in ids
