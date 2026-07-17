"""Shared fixtures for tests/agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import Tool, ToolCallResult
from primer.model.principal import PrincipalRef


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

    def required_role(self, tool_name: str) -> str:
        del tool_name
        return "admin"

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx=None,
    ) -> ToolCallResult:
        return ToolCallResult(output=str(arguments), is_error=False)


@pytest.fixture
def tool_manager_with_test_tools() -> ToolExecutionManager:
    """Return a ToolExecutionManager pre-populated with a '_test__echo' tool."""
    provider = _EchoProvider()
    return ToolExecutionManager(
        toolset_providers={"_test": provider},  # type: ignore[arg-type]
        # System invoker clears the RBAC tool floor on the toolset dispatch
        # path (a None invoker fails closed and denies every call).
        initiated_by=PrincipalRef.system(),
    )
