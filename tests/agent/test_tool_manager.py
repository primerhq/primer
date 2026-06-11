"""Tests for primer.agent.tool_manager.ToolExecutionManager."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

# Importing primer.workspace ensures ToolCallContext.model_rebuild() runs so
# the forward reference to AgentSession resolves before the manager
# constructs a context.
import primer.workspace  # noqa: F401
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import (
    Tool,
    ToolCallPart,
    ToolCallResult,
    ToolResultPart,
)
from primer.model.except_ import (
    AuthRequiredError,
    ConfigError,
    ProviderError,
    UnsupportedContentError,
)


# ---- Fakes ----------------------------------------------------------------


class _FakeToolsetProvider:
    """Fake :class:`ToolsetProvider` implementing the structural protocol."""

    def __init__(self, *, toolset_id: str, tools: list[Tool]) -> None:
        self._toolset_id = toolset_id
        self._tools = tools
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

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
    ) -> ToolCallResult:
        self.calls.append((tool_name, arguments, principal))
        return ToolCallResult(output=f"{tool_name}({arguments})", is_error=False)


def _tool(name: str, *, toolset_id: str) -> Tool:
    return Tool(
        id=name,
        description=f"a test tool named {name}",
        toolset_id=toolset_id,
        args_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )


# ---- Toolset-provider routing ---------------------------------------------


class TestToolsetRouting:
    @pytest.mark.asyncio
    async def test_list_tools_merges_providers(self) -> None:
        a = _FakeToolsetProvider(toolset_id="a", tools=[_tool("foo", toolset_id="a")])
        b = _FakeToolsetProvider(toolset_id="b", tools=[_tool("bar", toolset_id="b")])
        mgr = ToolExecutionManager(toolset_providers={"a": a, "b": b})  # type: ignore[arg-type]
        tools = await mgr.list_tools()
        names = sorted(t.id for t in tools)
        # Tool ids are scoped as ``toolset_id__bare_name`` so collisions
        # across toolsets are impossible.
        assert names == ["a__foo", "b__bar"]

    @pytest.mark.asyncio
    async def test_execute_routes_to_owning_toolset(self) -> None:
        a = _FakeToolsetProvider(toolset_id="a", tools=[_tool("foo", toolset_id="a")])
        b = _FakeToolsetProvider(toolset_id="b", tools=[_tool("bar", toolset_id="b")])
        mgr = ToolExecutionManager(toolset_providers={"a": a, "b": b})  # type: ignore[arg-type]
        await mgr.list_tools()
        call = ToolCallPart(id="c-1", name="a__foo", arguments={"x": 1})
        result = await mgr.execute(call)
        assert isinstance(result, ToolResultPart)
        assert result.id == "c-1"
        assert "foo" in result.output
        assert a.calls == [("foo", {"x": 1}, None)]
        assert b.calls == []

    @pytest.mark.asyncio
    async def test_execute_passes_principal_through(self) -> None:
        a = _FakeToolsetProvider(toolset_id="a", tools=[_tool("foo", toolset_id="a")])
        mgr = ToolExecutionManager(toolset_providers={"a": a})  # type: ignore[arg-type]
        await mgr.list_tools()
        await mgr.execute(
            ToolCallPart(id="c", name="a__foo", arguments={}),
            principal="alice@example.com",
        )
        assert a.calls == [("foo", {}, "alice@example.com")]

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_raises_unsupported(self) -> None:
        a = _FakeToolsetProvider(toolset_id="a", tools=[_tool("foo", toolset_id="a")])
        mgr = ToolExecutionManager(toolset_providers={"a": a})  # type: ignore[arg-type]
        await mgr.list_tools()
        with pytest.raises(UnsupportedContentError):
            await mgr.execute(ToolCallPart(id="c", name="not_a_tool", arguments={}))

    @pytest.mark.asyncio
    async def test_execute_propagates_provider_error_as_tool_result(self) -> None:
        class _Boom(_FakeToolsetProvider):
            async def call(self, *, tool_name, arguments, principal=None, ctx=None):
                raise ProviderError("upstream broke")

        a = _Boom(toolset_id="a", tools=[_tool("foo", toolset_id="a")])
        mgr = ToolExecutionManager(toolset_providers={"a": a})  # type: ignore[arg-type]
        await mgr.list_tools()
        result = await mgr.execute(ToolCallPart(id="c", name="a__foo", arguments={}))
        assert result.error is True
        assert "upstream broke" in result.output

    @pytest.mark.asyncio
    async def test_execute_propagates_auth_required(self) -> None:
        class _NeedAuth(_FakeToolsetProvider):
            async def call(self, *, tool_name, arguments, principal=None, ctx=None):
                raise AuthRequiredError(
                    "oauth needed",
                    auth_url="https://example.com/oauth",
                    state="opaque",
                )

        a = _NeedAuth(toolset_id="a", tools=[_tool("foo", toolset_id="a")])
        mgr = ToolExecutionManager(toolset_providers={"a": a})  # type: ignore[arg-type]
        await mgr.list_tools()
        with pytest.raises(AuthRequiredError):
            await mgr.execute(ToolCallPart(id="c", name="a__foo", arguments={}))

    @pytest.mark.asyncio
    async def test_execute_lazy_lists_tools_when_not_called_first(self) -> None:
        a = _FakeToolsetProvider(toolset_id="a", tools=[_tool("foo", toolset_id="a")])
        mgr = ToolExecutionManager(toolset_providers={"a": a})  # type: ignore[arg-type]
        result = await mgr.execute(ToolCallPart(id="c", name="a__foo", arguments={}))
        assert result.id == "c"


# ---- Workspace-tool routing -----------------------------------------------


class TestAgentToolFilter:
    """``tools`` narrows the manager to exactly the scoped ids the
    agent registered — never a whole toolset. Mirrors what the worker
    + chat WS handler pass when building the manager from
    :attr:`Agent.tools`."""

    @pytest.mark.asyncio
    async def test_list_tools_filters_to_registered_scoped_ids(self) -> None:
        a = _FakeToolsetProvider(
            toolset_id="a",
            tools=[
                _tool("foo", toolset_id="a"),
                _tool("bar", toolset_id="a"),
                _tool("baz", toolset_id="a"),
            ],
        )
        mgr = ToolExecutionManager(
            toolset_providers={"a": a},  # type: ignore[arg-type]
            tools=["a__foo", "a__baz"],
        )
        tools = await mgr.list_tools()
        assert sorted(t.id for t in tools) == ["a__baz", "a__foo"]

    @pytest.mark.asyncio
    async def test_execute_rejects_unregistered_tool(self) -> None:
        a = _FakeToolsetProvider(
            toolset_id="a",
            tools=[_tool("foo", toolset_id="a"), _tool("bar", toolset_id="a")],
        )
        mgr = ToolExecutionManager(
            toolset_providers={"a": a},  # type: ignore[arg-type]
            tools=["a__foo"],
        )
        await mgr.list_tools()
        # a__foo is registered and routes correctly.
        ok = await mgr.execute(ToolCallPart(id="c1", name="a__foo", arguments={}))
        assert ok.error is False
        assert a.calls == [("foo", {}, None)]
        # a__bar is provided by the toolset but the agent didn't list
        # it — must be refused so the operator's narrowed surface
        # actually load-bears.
        with pytest.raises(
            UnsupportedContentError, match="not in the agent's registered tool list",
        ):
            await mgr.execute(ToolCallPart(id="c2", name="a__bar", arguments={}))

    @pytest.mark.asyncio
    async def test_none_tools_keeps_unfiltered_behaviour(self) -> None:
        """``tools=None`` keeps the legacy 'no filter' semantic for
        non-agent callers (graph executors using a parent manager,
        tests that want to enumerate everything a toolset exposes).
        Agent paths always pass ``agent.tools`` explicitly so they
        never hit this branch."""
        a = _FakeToolsetProvider(
            toolset_id="a",
            tools=[_tool("foo", toolset_id="a"), _tool("bar", toolset_id="a")],
        )
        mgr = ToolExecutionManager(
            toolset_providers={"a": a},  # type: ignore[arg-type]
            tools=None,
        )
        tools = await mgr.list_tools()
        assert sorted(t.id for t in tools) == ["a__bar", "a__foo"]


class _FakeWorkspaceTool:
    """Fake :class:`WorkspaceTool` implementing the structural protocol."""

    id = "fake_ws_tool"
    description = "a fake workspace tool"
    examples: list = []
    requires_workspace_context = True

    def __init__(self) -> None:
        self.executed: list[Any] = []

    def parameters(self):
        from pydantic import BaseModel

        class _Args(BaseModel):
            x: int = 0

        return _Args

    async def execute(self, args, ctx):
        self.executed.append((args, ctx))
        from primer.workspace.tool import ToolResult

        return ToolResult(output=f"ws({args.x})", metadata={}, truncated=False)


class _FakeAgentSession:
    """Bare-minimum stand-in for :class:`AgentSession`."""

    workspace_id = "ws-1"
    session_id = "sess-1"
    agent_id = "agent-1"
    workspace_tools: list = []

    def __init__(self) -> None:
        self.cached: list[str] = []

    async def cache_output(self, text: str) -> str:
        self.cached.append(text)
        return f"/tmp/cache/{len(self.cached)}.txt"


class TestWorkspaceRouting:
    @pytest.mark.asyncio
    async def test_workspace_tool_listed_with_synthetic_toolset_id(self) -> None:
        ws = _FakeWorkspaceTool()
        sess = _FakeAgentSession()
        mgr = ToolExecutionManager(
            workspace_tools={"fake_ws_tool": ws},  # type: ignore[arg-type]
            workspace_session=sess,  # type: ignore[arg-type]
        )
        tools = await mgr.list_tools()
        assert len(tools) == 1
        assert tools[0].id == "workspace__fake_ws_tool"
        assert tools[0].toolset_id == "workspace"

    @pytest.mark.asyncio
    async def test_workspace_tool_dispatch(self) -> None:
        ws = _FakeWorkspaceTool()
        sess = _FakeAgentSession()
        mgr = ToolExecutionManager(
            workspace_tools={"fake_ws_tool": ws},  # type: ignore[arg-type]
            workspace_session=sess,  # type: ignore[arg-type]
        )
        await mgr.list_tools()
        result = await mgr.execute(
            ToolCallPart(id="c", name="workspace__fake_ws_tool", arguments={"x": 7})
        )
        assert result.output == "ws(7)"
        assert len(ws.executed) == 1

    @pytest.mark.asyncio
    async def test_workspace_tool_invalid_args_returns_error_result(self) -> None:
        ws = _FakeWorkspaceTool()
        sess = _FakeAgentSession()
        mgr = ToolExecutionManager(
            workspace_tools={"fake_ws_tool": ws},  # type: ignore[arg-type]
            workspace_session=sess,  # type: ignore[arg-type]
        )
        await mgr.list_tools()
        result = await mgr.execute(
            ToolCallPart(id="c", name="workspace__fake_ws_tool", arguments={"x": "not_an_int"})
        )
        assert result.error is True
        assert "invalid arguments" in result.output

    @pytest.mark.asyncio
    async def test_workspace_tool_truncation_envelope(self) -> None:
        big = "x" * (60 * 1024)

        class _BigTool(_FakeWorkspaceTool):
            async def execute(self, args, ctx):
                from primer.workspace.tool import ToolResult

                return ToolResult(output=big, metadata={}, truncated=False)

        ws = _BigTool()
        sess = _FakeAgentSession()
        mgr = ToolExecutionManager(
            workspace_tools={"fake_ws_tool": ws},  # type: ignore[arg-type]
            workspace_session=sess,  # type: ignore[arg-type]
        )
        await mgr.list_tools()
        result = await mgr.execute(
            ToolCallPart(id="c", name="workspace__fake_ws_tool", arguments={"x": 0})
        )
        assert "[the tool succeeded but the output was truncated]" in result.output
        assert sess.cached == [big]

    @pytest.mark.asyncio
    async def test_workspace_tool_under_threshold_passthrough(self) -> None:
        ws = _FakeWorkspaceTool()
        sess = _FakeAgentSession()
        mgr = ToolExecutionManager(
            workspace_tools={"fake_ws_tool": ws},  # type: ignore[arg-type]
            workspace_session=sess,  # type: ignore[arg-type]
        )
        await mgr.list_tools()
        result = await mgr.execute(
            ToolCallPart(id="c", name="workspace__fake_ws_tool", arguments={"x": 7})
        )
        assert result.output == "ws(7)"
        assert sess.cached == []

    def test_workspace_tools_without_session_rejected(self) -> None:
        ws = _FakeWorkspaceTool()
        with pytest.raises(ConfigError):
            ToolExecutionManager(
                workspace_tools={"fake_ws_tool": ws},  # type: ignore[arg-type]
                workspace_session=None,
            )


# ---- inform sink plumbing -------------------------------------------------


class _CapturingProvider:
    """Toolset provider that records the ``ctx`` it was dispatched with."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def list_tools(self, *, principal: str | None = None):
        if False:  # pragma: no cover - makes this an async generator
            yield None

    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        self.captured["inform"] = getattr(ctx, "inform", "MISSING")
        return ToolCallResult(output="{}", is_error=False)


@pytest.mark.asyncio
async def test_set_inform_sink_passed_into_tool_context() -> None:
    provider = _CapturingProvider()
    mgr = ToolExecutionManager(
        toolset_providers={"misc": provider},  # type: ignore[arg-type]
        chat_id="chat-1",
    )

    async def _sink(msg: str) -> int:
        return 1

    mgr.set_inform_sink(_sink)

    await mgr._dispatch_toolset(
        ToolCallPart(id="tc1", name="misc__x", arguments={}),
        toolset_id="misc",
        bare_name="x",
        principal=None,
    )
    assert provider.captured["inform"] is _sink
