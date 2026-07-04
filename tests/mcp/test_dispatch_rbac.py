"""Task 9 — MCP dispatch RBAC: reserved ``system`` mutation tools are
admin-only, even when the operator allow-listed them.

The gate lives in :func:`primer.mcp.dispatch.invoke_exposed` (before the
provider is ever touched) and is fed the caller's role via the new
``actor`` keyword, which the MCP handler pulls off
:data:`primer.mcp.server.current_actor`.
"""

from __future__ import annotations

import pytest

from primer.mcp.dispatch import NotExposed, invoke_exposed
from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import current_actor
from primer.model.chat import Tool, ToolCallResult
from primer.model.principal import Principal


class _SysProvider:
    """Minimal ToolsetProvider stand-in for the reserved ``system`` id."""

    toolset_id = "system"

    def __init__(self, tools: list[Tool]) -> None:
        self._tools = tools
        self.calls: list[str] = []

    async def list_tools(self, *, principal=None):
        del principal
        for tool in self._tools:
            yield tool

    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        del arguments, principal, ctx
        self.calls.append(tool_name)
        return ToolCallResult(output="ok", is_error=False)

    def is_yielding(self, tool_name: str) -> bool:
        del tool_name
        return False

    def requires_session(self, tool_name: str) -> bool:
        del tool_name
        return False


class _Registry:
    def __init__(self, providers: dict[str, _SysProvider]) -> None:
        self._providers = providers

    async def get_toolset(self, toolset_id: str):
        return self._providers.get(toolset_id)


def _system_deps(storage):
    tool = Tool(
        id="create_agent",
        toolset_id="system",
        description="Create an agent.",
        args_schema={"type": "object", "properties": {}},
    )
    provider = _SysProvider([tool])
    registry = _Registry({"system": provider})
    deps = ExposureDeps(storage_provider=storage, provider_registry=registry)
    return deps, provider


@pytest.mark.asyncio
async def test_system_mutation_tool_refused_for_user_role(
    fake_storage_provider,
) -> None:
    """A user-role MCP caller invoking ``system__create_agent`` is refused
    (``forbidden_role``) before the provider is dispatched."""
    deps, provider = _system_deps(fake_storage_provider)
    await update_exposure(
        enabled=True, allowed_tools=["system__create_agent"],
        updated_by="admin", deps=deps,
    )
    actor = Principal(
        type="user", id="u", display="u", role="user", source="local",
    )

    with pytest.raises(NotExposed) as excinfo:
        await invoke_exposed(
            scoped_id="system__create_agent", arguments={},
            principal="u", actor=actor, deps=deps,
        )

    assert excinfo.value.reason == "forbidden_role"
    assert provider.calls == []  # never dispatched


@pytest.mark.asyncio
async def test_system_mutation_tool_allowed_for_admin_role(
    fake_storage_provider,
) -> None:
    """An admin-role caller passes the gate and the tool runs."""
    deps, provider = _system_deps(fake_storage_provider)
    await update_exposure(
        enabled=True, allowed_tools=["system__create_agent"],
        updated_by="admin", deps=deps,
    )
    actor = Principal(
        type="user", id="a", display="a", role="admin", source="local",
    )

    result = await invoke_exposed(
        scoped_id="system__create_agent", arguments={},
        principal="a", actor=actor, deps=deps,
    )

    assert result.is_error is False
    assert provider.calls == ["create_agent"]


def test_current_actor_defaults_to_none() -> None:
    """The new ContextVar defaults to None for tests / dev loops."""
    assert current_actor.get() is None
