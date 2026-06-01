"""invoke_one helper — Spec §9.

The MCP server endpoint calls tools directly without an agent context, so
``invoke_one`` extracts only the dispatch + metrics tail of
``ToolExecutionManager._execute_inner``: no per-agent allowlist, no
approval gate, no workspace-tool branch. These tests pin down that
behavioural contract.
"""

from __future__ import annotations

import pytest

from primer.agent.tool_manager import invoke_one
from primer.model.chat import ToolCallResult


class _OkProvider:
    async def call(self, *, tool_name, arguments, principal, ctx):
        return ToolCallResult(output=f"{tool_name}:{arguments['x']}", is_error=False)


class _BoomProvider:
    async def call(self, *, tool_name, arguments, principal, ctx):
        raise ValueError("boom")


@pytest.mark.asyncio
async def test_invoke_one_returns_tool_call_result():
    result = await invoke_one(
        provider=_OkProvider(),
        tool_name="echo",
        arguments={"x": "hello"},
        principal="alice",
    )
    assert result.output == "echo:hello"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_invoke_one_propagates_exceptions():
    with pytest.raises(ValueError):
        await invoke_one(
            provider=_BoomProvider(),
            tool_name="x",
            arguments={},
            principal=None,
        )


@pytest.mark.asyncio
async def test_invoke_one_passes_principal_and_ctx_none():
    """The MCP path NEVER has a ctx; verify provider.call is invoked with ctx=None."""
    captured: dict = {}

    class _Capture:
        async def call(self, *, tool_name, arguments, principal, ctx):
            captured["principal"] = principal
            captured["ctx"] = ctx
            return ToolCallResult(output="ok", is_error=False)

    await invoke_one(
        provider=_Capture(),
        tool_name="x",
        arguments={},
        principal="bob",
    )
    assert captured["principal"] == "bob"
    assert captured["ctx"] is None
