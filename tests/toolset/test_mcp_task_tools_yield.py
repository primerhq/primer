"""An MCP task-mode tool parks the turn, so the provider says it yields (AG-M22).

``McpToolsetProvider.is_yielding`` and the tool descriptor's ``yields`` flag are True for a tool that advertises
task support (the provider routes it through the yielding path), and stay False for every other tool. The MCP
exposure filter's answer for an operator MCP toolset's tools is unchanged (it never exposes them).
"""
from __future__ import annotations

import mcp.types as mcp_types

from primer.mcp.safety import is_exposable
from tests.toolset.test_mcp_tasks import _FakeSession, _FakeStdioProvider


def _tools() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(name="required", description="d", inputSchema={"type": "object"},
                       execution=mcp_types.ToolExecution(taskSupport="required")),
        mcp_types.Tool(name="optional", description="d", inputSchema={"type": "object"},
                       execution=mcp_types.ToolExecution(taskSupport="optional")),
        mcp_types.Tool(name="forbidden", description="d", inputSchema={"type": "object"},
                       execution=mcp_types.ToolExecution(taskSupport="forbidden")),
        mcp_types.Tool(name="plain", description="d", inputSchema={"type": "object"}),
    ]


async def _listed():
    provider = _FakeStdioProvider(_FakeSession(tools=_tools()), toolset_id="srv")
    listed = [t async for t in provider.list_tools()]
    return provider, {t.id: t for t in listed}


# --- characterization ---------------------------------------------------------------------------------------------


async def test_non_task_tools_do_not_yield() -> None:
    provider, listed = await _listed()
    for name in ("forbidden", "plain"):
        assert provider.is_yielding(name) is False
        assert listed[name].yields is False


async def test_exposure_answer_is_unchanged_for_every_tool() -> None:
    provider, listed = await _listed()
    for tool in listed.values():
        assert is_exposable(tool, provider=provider) == (False, "not_system_toolset")


async def test_nothing_yields_before_a_listing() -> None:
    provider = _FakeStdioProvider(_FakeSession(tools=_tools()), toolset_id="srv")
    assert provider.is_yielding("required") is False


# --- red ----------------------------------------------------------------------------------------------------------


async def test_task_tools_yield() -> None:
    provider, listed = await _listed()
    for name in ("required", "optional"):
        assert provider.is_yielding(name) is True
        assert listed[name].yields is True
