"""Shared fixtures for tests/agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from matrix.agent.tool_manager import ToolExecutionManager
from matrix.model.chat import Tool, ToolCallResult


class _EchoProvider:
    """Minimal fake ToolsetProvider for toolset_id '_test', tool 'echo'."""

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(
            id="echo",
            description="echoes its arguments",
            toolset_id="_test",
            args_schema={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "additionalProperties": False,
            },
        )

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
    ) -> ToolCallResult:
        return ToolCallResult(output=str(arguments), is_error=False)


@pytest.fixture
def tool_manager_with_test_tools() -> ToolExecutionManager:
    """Return a ToolExecutionManager pre-populated with a '_test__echo' tool."""
    provider = _EchoProvider()
    return ToolExecutionManager(toolset_providers={"_test": provider})  # type: ignore[arg-type]
