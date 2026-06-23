"""Fixtures shared by the MCP exposure / safety tests.

Provides ``fake_provider_registry_with_tools`` -- a registry stub that
emits a handful of tools across a couple of toolset ids so the exposure
service can iterate the catalogue without the real provider plumbing.

Intentionally NOT exporting an ``__init__.py`` for ``tests/mcp/`` --
pytest's rootdir-relative collection works without it, and adding one
would shadow the third-party ``mcp`` SDK package on the import path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from collections.abc import Callable

import pytest

from primer.model.chat import Tool, ToolCallResult


class FakeToolsetProvider:
    """ToolsetProvider stand-in that yields a fixed list of tools.

    Both safety probes (``is_yielding`` / ``requires_session``) consult a
    per-tool override map so individual tests can flip a single tool's
    flag without subclassing.
    """

    def __init__(
        self,
        toolset_id: str,
        tools: list[Tool],
        *,
        yielding: set[str] | None = None,
        sessioned: set[str] | None = None,
        call_handler: Callable[..., ToolCallResult] | None = None,
    ) -> None:
        self.toolset_id = toolset_id
        self._tools = tools
        self._yielding = yielding or set()
        self._sessioned = sessioned or set()
        self._call_handler = call_handler
        self.calls: list[dict[str, Any]] = []

    async def list_tools(
        self, *, principal: str | None = None,
    ) -> AsyncIterator[Tool]:
        del principal  # unused
        for tool in self._tools:
            yield tool

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx: Any = None,
    ) -> ToolCallResult:
        """Record the invocation and delegate to ``call_handler``.

        Defaults to echoing the bare tool name + arguments so the
        dispatch tests can assert end-to-end plumbing without wiring
        up a per-test handler.
        """
        self.calls.append({
            "tool_name": tool_name,
            "arguments": arguments,
            "principal": principal,
            "ctx": ctx,
        })
        if self._call_handler is not None:
            return self._call_handler(
                tool_name=tool_name,
                arguments=arguments,
                principal=principal,
                ctx=ctx,
            )
        return ToolCallResult(
            output=f"{tool_name}:{arguments}", is_error=False,
        )

    def is_yielding(self, tool_name: str) -> bool:
        return tool_name in self._yielding

    def requires_session(self, tool_name: str) -> bool:
        return tool_name in self._sessioned


class FakeProviderRegistry:
    """Registry shim driving ``_iter_catalogue`` without real providers.

    ``RESERVED_TOOLSET_IDS`` is read by the exposure module from the
    *real* registry module, so the stub here only needs to satisfy
    ``await registry.get_toolset(toolset_id)``. Unknown ids return
    ``None`` so the iterator skips them silently.
    """

    def __init__(self, providers: dict[str, FakeToolsetProvider]) -> None:
        self._providers = providers

    async def get_toolset(self, toolset_id: str) -> FakeToolsetProvider | None:
        return self._providers.get(toolset_id)


def _make_tool(toolset_id: str, name: str, descr: str = "") -> Tool:
    return Tool(
        id=name,
        toolset_id=toolset_id,
        description=descr or f"{toolset_id}.{name}",
        args_schema={"type": "object", "properties": {}},
    )


@pytest.fixture
def fake_misc_tools() -> list[Tool]:
    return [
        _make_tool("misc", "uuid_v4", "Generate a random UUIDv4."),
        _make_tool("misc", "now", "Return the current UTC timestamp."),
    ]


@pytest.fixture
def fake_provider_registry_with_tools(
    fake_misc_tools: list[Tool],
) -> Any:
    """A registry whose only built-in id ``misc`` exposes two safe tools."""
    provider = FakeToolsetProvider("misc", fake_misc_tools)
    return FakeProviderRegistry({"misc": provider})


@pytest.fixture
def fake_provider_registry_with_yielding(
    fake_misc_tools: list[Tool],
) -> Any:
    """Same misc toolset, but ``uuid_v4`` is flagged yielding.

    Used by ``test_update_exposure_rejects_non_exposable_id`` -- the
    safety predicate must refuse to allow a yielding tool.
    """
    provider = FakeToolsetProvider(
        "misc",
        fake_misc_tools,
        yielding={"uuid_v4"},
    )
    return FakeProviderRegistry({"misc": provider})


__all__ = [
    "FakeProviderRegistry",
    "FakeToolsetProvider",
]
