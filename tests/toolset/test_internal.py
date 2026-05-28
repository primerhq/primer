"""Tests for matrix.toolset.internal.InternalToolsetProvider."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import ConfigError, UnsupportedContentError
from primer.toolset.internal import InternalToolsetProvider


def _make_tool(name: str, toolset_id: str = "ts1") -> Tool:
    return Tool(
        id=name,
        description=f"tool {name}",
        toolset_id=toolset_id,
        args_schema={"type": "object", "properties": {}},
    )


async def _ok_handler(args: dict[str, Any]) -> ToolCallResult:
    return ToolCallResult(output=f"ok:{sorted(args.items())}")


async def _err_handler(args: dict[str, Any]) -> ToolCallResult:
    return ToolCallResult(output="boom", is_error=True)


class TestConstructor:
    def test_empty_registry_is_allowed(self) -> None:
        InternalToolsetProvider(toolset_id="ts1", registry={})

    def test_mismatched_toolset_id_raises_config_error(self) -> None:
        tool = _make_tool("foo", toolset_id="other")
        with pytest.raises(ConfigError) as exc_info:
            InternalToolsetProvider(
                toolset_id="ts1",
                registry={"foo": (tool, _ok_handler)},
            )
        assert "toolset_id" in str(exc_info.value).lower()

    def test_registry_is_copied_not_aliased(self) -> None:
        original = {"foo": (_make_tool("foo"), _ok_handler)}
        provider = InternalToolsetProvider(toolset_id="ts1", registry=original)
        original["bar"] = (_make_tool("bar"), _ok_handler)

        names: list[str] = []

        async def collect() -> None:
            async for t in provider.list_tools():
                names.append(t.id)

        asyncio.run(collect())
        assert names == ["foo"]


class TestListTools:
    async def test_yields_each_tool_in_insertion_order(self) -> None:
        tools = [_make_tool("a"), _make_tool("b"), _make_tool("c")]
        registry = {t.id: (t, _ok_handler) for t in tools}
        provider = InternalToolsetProvider(toolset_id="ts1", registry=registry)

        seen = [t.id async for t in provider.list_tools()]
        assert seen == ["a", "b", "c"]

    async def test_principal_argument_is_ignored(self) -> None:
        provider = InternalToolsetProvider(
            toolset_id="ts1",
            registry={"foo": (_make_tool("foo"), _ok_handler)},
        )
        seen_default = [t.id async for t in provider.list_tools()]
        seen_with_principal = [
            t.id async for t in provider.list_tools(principal="user-42")
        ]
        assert seen_default == seen_with_principal == ["foo"]


class TestCall:
    async def test_dispatch_runs_handler_and_returns_result(self) -> None:
        provider = InternalToolsetProvider(
            toolset_id="ts1",
            registry={"foo": (_make_tool("foo"), _ok_handler)},
        )
        result = await provider.call(tool_name="foo", arguments={"x": 1})
        assert isinstance(result, ToolCallResult)
        assert result.output.startswith("ok:")
        assert result.is_error is False

    async def test_unknown_tool_raises_unsupported_content_error(self) -> None:
        provider = InternalToolsetProvider(toolset_id="ts1", registry={})
        with pytest.raises(UnsupportedContentError) as exc_info:
            await provider.call(tool_name="missing", arguments={})
        assert "missing" in str(exc_info.value)
        assert "ts1" in str(exc_info.value)

    async def test_handler_can_return_an_error_result(self) -> None:
        provider = InternalToolsetProvider(
            toolset_id="ts1",
            registry={"foo": (_make_tool("foo"), _err_handler)},
        )
        result = await provider.call(tool_name="foo", arguments={})
        assert result.is_error is True
        assert result.output == "boom"
