"""Task 3 — MCP dispatch RBAC: every exposed tool is gated on its
declared ``required_role`` (read from the owning provider), not the old
``system``-toolset name-prefix heuristic.

The gate lives in :func:`primer.mcp.dispatch.invoke_exposed` (after the
tool is resolved + confirmed exposable, before the handler ever runs)
and is fed the caller's role via the ``actor`` keyword, which the MCP
handler pulls off :data:`primer.mcp.server.current_actor`. Denial is
returned IN-BAND as an ``is_error`` :class:`ToolCallResult` — the tool
genuinely exists and is exposed, this is a per-caller authorization
failure, not ``not_exposed`` — and the handler is never invoked.
"""

from __future__ import annotations

import pytest

from primer.mcp.dispatch import invoke_exposed
from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import current_actor
from primer.model.chat import Tool, ToolCallResult
from primer.model.principal import Principal


class _SysProvider:
    """Minimal ToolsetProvider stand-in for the reserved ``system`` id."""

    toolset_id = "system"

    def __init__(self, tools: list[Tool], roles: dict[str, str]) -> None:
        self._tools = tools
        self._roles = roles
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

    def required_role(self, tool_name: str) -> str:
        return self._roles.get(tool_name, "admin")


class _Registry:
    def __init__(self, providers: dict[str, _SysProvider]) -> None:
        self._providers = providers

    async def get_toolset(self, toolset_id: str):
        return self._providers.get(toolset_id)


def _system_deps(storage):
    """Three reserved tools: ``restricted``-, ``user``-, and ``admin``-role."""
    tools = [
        Tool(
            id="ping",
            toolset_id="system",
            description="Ungated liveness probe.",
            args_schema={"type": "object", "properties": {}},
        ),
        Tool(
            id="create_agent",
            toolset_id="system",
            description="Create an agent.",
            args_schema={"type": "object", "properties": {}},
        ),
        Tool(
            id="create_llm_provider",
            toolset_id="system",
            description="Create an LLM provider.",
            args_schema={"type": "object", "properties": {}},
        ),
    ]
    roles = {
        "ping": "restricted",
        "create_agent": "user",
        "create_llm_provider": "admin",
    }
    provider = _SysProvider(tools, roles)
    registry = _Registry({"system": provider})
    deps = ExposureDeps(storage_provider=storage, provider_registry=registry)
    return deps, provider


async def _invoke(deps, *, tool_name: str, actor: Principal | None):
    await update_exposure(
        enabled=True, allowed_tools=[f"system__{tool_name}"],
        updated_by="admin", deps=deps,
    )
    return await invoke_exposed(
        scoped_id=f"system__{tool_name}", arguments={},
        principal=getattr(actor, "id", None), actor=actor, deps=deps,
    )


@pytest.mark.parametrize(
    ("role", "tool_name", "allowed"),
    [
        ("user", "create_agent", True),
        ("user", "create_llm_provider", False),
        ("admin", "create_agent", True),
        ("admin", "create_llm_provider", True),
        ("restricted", "create_agent", False),
        ("restricted", "create_llm_provider", False),
    ],
)
@pytest.mark.asyncio
async def test_role_matrix_for_user_type_actor(
    fake_storage_provider, role, tool_name, allowed,
) -> None:
    """A ``user``-type actor is gated strictly by ``_ROLE_RANK`` against
    the tool's declared role; the handler runs iff the rank clears."""
    deps, provider = _system_deps(fake_storage_provider)
    actor = Principal(
        type="user", id="u", display="u", role=role, source="local",
    )

    result = await _invoke(deps, tool_name=tool_name, actor=actor)

    if allowed:
        assert result.is_error is False
        assert provider.calls == [tool_name]
    else:
        assert result.is_error is True
        assert "requires" in result.output
        assert provider.calls == []  # never dispatched


@pytest.mark.asyncio
async def test_restricted_actor_allowed_on_restricted_role_tool(
    fake_storage_provider,
) -> None:
    """A ``restricted`` actor -- now free to CONNECT at all, per the
    connect-time gate change (Task 9 follow-up) -- clears a tool whose
    declared role is itself ``restricted`` (rank 0 meets rank 0), while a
    ``user``-role tool in the SAME call session is still denied. Proves
    the per-call ``required_role`` floor is the only gate now, and that
    it is not a blanket "restricted can never call anything" rule."""
    deps, provider = _system_deps(fake_storage_provider)
    actor = Principal(
        type="user", id="r", display="r", role="restricted", source="local",
    )

    ok_result = await _invoke(deps, tool_name="ping", actor=actor)
    denied_result = await _invoke(deps, tool_name="create_agent", actor=actor)

    assert ok_result.is_error is False
    assert provider.calls == ["ping"]
    assert denied_result.is_error is True
    assert "requires" in denied_result.output


@pytest.mark.asyncio
async def test_system_type_actor_always_allowed(fake_storage_provider) -> None:
    """A system-type Principal (the auth-disabled bypass) clears the gate
    for the admin-role tool too, regardless of its (typically unset)
    ``role``."""
    deps, provider = _system_deps(fake_storage_provider)
    actor = Principal(
        type="system", id="s", display="s", role=None, source="system",
    )

    result = await _invoke(deps, tool_name="create_llm_provider", actor=actor)

    assert result.is_error is False
    assert provider.calls == ["create_llm_provider"]


@pytest.mark.asyncio
async def test_missing_actor_is_denied(fake_storage_provider) -> None:
    """``actor=None`` (no auth context reached the dispatcher) is refused
    even for the lowest-privilege ``user``-role tool."""
    deps, provider = _system_deps(fake_storage_provider)

    result = await _invoke(deps, tool_name="create_agent", actor=None)

    assert result.is_error is True
    assert provider.calls == []


def test_current_actor_defaults_to_none() -> None:
    """The new ContextVar defaults to None for tests / dev loops."""
    assert current_actor.get() is None
