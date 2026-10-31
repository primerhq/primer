"""The MCP catalogue shows a caller only reserved built-in tools (access-control P5 Task 4, lead correction).

``is_exposable`` refuses every tool whose toolset is not a reserved built-in, at the allowlist save, at the list and at the call, and a stored ``Toolset``
row cannot take a built-in id, so no owned (private, shared or public) toolset ever reaches an MCP caller. The list carries no person: it is the same
for a plain user and an admin, and a tool above the caller's role is refused at the call (``tests/mcp/test_dispatch_rbac.py``).
"""

from __future__ import annotations

import pytest

from primer.mcp.dispatch import NotExposed, invoke_exposed, list_exposed_tools
from primer.mcp.exposure import ExposureDeps, ToolNotExposable, update_exposure
from primer.model.access import Access
from primer.model.chat import Tool
from primer.model.principal import Principal
from primer.model.provider import Toolset, ToolsetProviderType
from tests.mcp.test_dispatch_rbac import _Registry, _SysProvider

_SCHEMA = {"type": "object", "properties": {}}


def _tool(toolset_id: str, name: str) -> Tool:
    return Tool(id=name, description=name, toolset_id=toolset_id, args_schema=_SCHEMA)


async def _deps(storage, *, public: bool):
    await storage.get_storage(Toolset).create(Toolset(
        id="mine", provider=ToolsetProviderType.INTERNAL, access=Access(owners=["u-owner"], public=public, public_tools=["secret"]),
    ))
    system = _SysProvider([_tool("system", "ping"), _tool("system", "create_llm_provider")], {"ping": "restricted", "create_llm_provider": "admin"})
    mine = _SysProvider([_tool("mine", "secret")], {"secret": "user"})
    return ExposureDeps(storage_provider=storage, provider_registry=_Registry({"system": system, "mine": mine})), system, mine


@pytest.mark.parametrize("public", [False, True], ids=["private", "public"])
@pytest.mark.asyncio
async def test_a_stored_toolset_can_never_be_exposed_or_listed_or_called(fake_storage_provider, public) -> None:
    deps, _system, mine = await _deps(fake_storage_provider, public=public)

    with pytest.raises(ToolNotExposable) as exc:
        await update_exposure(enabled=True, allowed_tools=["mine__secret"], updated_by="admin", deps=deps)
    assert "not_system_toolset" in str(exc.value)

    await update_exposure(enabled=True, allowed_tools=["system__ping"], updated_by="admin", deps=deps)
    assert [f"{t.toolset_id}__{t.id}" for t, _ in await list_exposed_tools(deps)] == ["system__ping"]
    with pytest.raises(NotExposed):
        await invoke_exposed(scoped_id="mine__secret", arguments={}, principal="u", actor=None, deps=deps)
    assert mine.calls == []


@pytest.mark.asyncio
async def test_the_list_is_the_same_for_a_plain_user_and_an_admin(fake_storage_provider) -> None:
    deps, system, _mine = await _deps(fake_storage_provider, public=False)
    await update_exposure(enabled=True, allowed_tools=["system__ping", "system__create_llm_provider"], updated_by="admin", deps=deps)

    listed = sorted(f"{t.toolset_id}__{t.id}" for t, _ in await list_exposed_tools(deps))
    user = Principal(type="user", id="u", display="u", role="user", source="local")
    admin = Principal(type="user", id="a", display="a", role="admin", source="local")
    denied = await invoke_exposed(scoped_id="system__create_llm_provider", arguments={}, principal="u", actor=user, deps=deps)
    allowed = await invoke_exposed(scoped_id="system__create_llm_provider", arguments={}, principal="a", actor=admin, deps=deps)

    assert listed == ["system__create_llm_provider", "system__ping"]
    assert denied.is_error and "requires" in denied.output and system.calls == ["create_llm_provider"]
    assert not allowed.is_error
