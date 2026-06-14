"""Conditional registration of the ``workspace_ext`` toolset.

The whole point of the ``workspace_ext`` toolset: an agent may bind it
like any other toolset, but its (context-heavy, workspace-only yielding)
tools are registered into the agent's live tool context ONLY when the
agent runs in a WORKSPACE SESSION. On a CHAT they are dropped at the
resolution choke point (:meth:`ToolExecutionManager.list_tools`) so they
never enter the chat's context window.

These tests exercise that choke point directly with a fake toolset
provider and the chat vs session context distinction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

# Resolve ToolCallContext's forward ref before the manager builds a context.
import primer.workspace  # noqa: F401
from primer.agent.tool_manager import (
    WORKSPACE_EXT_TOOLSET_ID,
    ToolExecutionManager,
)
from primer.model.chat import Tool, ToolCallResult


class _FakeToolsetProvider:
    def __init__(self, *, toolset_id: str, tools: list[Tool]) -> None:
        self._toolset_id = toolset_id
        self._tools = tools

    async def list_tools(
        self, *, principal: str | None = None
    ) -> AsyncIterator[Tool]:
        for t in self._tools:
            yield t

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx=None,
    ) -> ToolCallResult:  # pragma: no cover - not dispatched here
        return ToolCallResult(output="{}", is_error=False)


def _tool(name: str, *, toolset_id: str, yields: bool = False) -> Tool:
    return Tool(
        id=name,
        description=f"a test tool named {name}",
        toolset_id=toolset_id,
        args_schema={"type": "object", "properties": {}},
        yields=yields,
        requires_session=yields,
    )


class _FakeAgentSession:
    workspace_id = "ws-1"
    session_id = "sess-1"
    agent_id = "agent-1"
    workspace_tools: list = []


def _providers():
    """A misc toolset (always visible) + a workspace_ext toolset."""
    misc = _FakeToolsetProvider(
        toolset_id="misc",
        tools=[_tool("get_datetime", toolset_id="misc")],
    )
    we = _FakeToolsetProvider(
        toolset_id=WORKSPACE_EXT_TOOLSET_ID,
        tools=[
            _tool("watch_files", toolset_id=WORKSPACE_EXT_TOOLSET_ID, yields=True),
            _tool("sleep", toolset_id=WORKSPACE_EXT_TOOLSET_ID, yields=True),
        ],
    )
    return {"misc": misc, WORKSPACE_EXT_TOOLSET_ID: we}


# An agent that bound all four scoped ids - same agent in both contexts.
_AGENT_TOOLS = [
    "misc__get_datetime",
    "workspace_ext__watch_files",
    "workspace_ext__sleep",
]


@pytest.mark.asyncio
async def test_workspace_ext_dropped_on_chat_context():
    """Chat context (chat_id set, no session): workspace_ext tools dropped."""
    mgr = ToolExecutionManager(
        toolset_providers=_providers(),  # type: ignore[arg-type]
        tools=_AGENT_TOOLS,
        chat_id="chat-1",
    )
    ids = {t.id for t in await mgr.list_tools()}
    # The non-workspace_ext tool the agent bound is still there.
    assert "misc__get_datetime" in ids
    # The workspace_ext tools the agent bound are suppressed.
    assert "workspace_ext__watch_files" not in ids
    assert "workspace_ext__sleep" not in ids


@pytest.mark.asyncio
async def test_workspace_ext_registered_in_workspace_session():
    """Workspace session context: workspace_ext tools ARE registered."""
    mgr = ToolExecutionManager(
        toolset_providers=_providers(),  # type: ignore[arg-type]
        workspace_session=_FakeAgentSession(),  # type: ignore[arg-type]
        tools=_AGENT_TOOLS,
    )
    ids = {t.id for t in await mgr.list_tools()}
    assert "misc__get_datetime" in ids
    assert "workspace_ext__watch_files" in ids
    assert "workspace_ext__sleep" in ids


@pytest.mark.asyncio
async def test_suppressed_workspace_ext_call_is_rejected_on_chat():
    """A model that references a suppressed tool gets a clean rejection."""
    from primer.model.chat import ToolCallPart
    from primer.model.except_ import UnsupportedContentError

    mgr = ToolExecutionManager(
        toolset_providers=_providers(),  # type: ignore[arg-type]
        tools=_AGENT_TOOLS,
        chat_id="chat-1",
    )
    await mgr.list_tools()
    call = ToolCallPart(
        id="tc-1", name="workspace_ext__watch_files", arguments={}
    )
    with pytest.raises(UnsupportedContentError):
        await mgr.execute(call)


@pytest.mark.asyncio
async def test_for_workspace_keeps_workspace_ext():
    """The ``for_workspace`` constructor path registers workspace_ext too."""
    mgr = ToolExecutionManager.for_workspace(
        toolset_providers=_providers(),  # type: ignore[arg-type]
        session=_FakeAgentSession(),  # type: ignore[arg-type]
        tools=_AGENT_TOOLS,
    )
    ids = {t.id for t in await mgr.list_tools()}
    assert "workspace_ext__watch_files" in ids
    assert "workspace_ext__sleep" in ids
