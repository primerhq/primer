"""A privately owned toolset's tools never reach an MCP caller (access-control spec 9).

The MCP surface exposes only the reserved built-in toolsets (``is_exposable``'s
system-only floor, ``not_system_toolset``); a stored Toolset row, the only kind
with an owner and grants, never gets there. That floor is what keeps a user's
private toolset off ``POST /v1/mcp`` without a per-caller toolset access check,
so this pins it at the server boundary: an admin cannot allowlist such a tool,
and an allowlist that names one anyway (a row written past the PUT validation)
shows it to nobody and answers its call exactly like a tool that does not exist.
If the floor is ever relaxed, these fail: add a per-caller toolset access filter
to ``list_exposed_tools`` / ``invoke_exposed`` before letting them pass.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import METHOD_NOT_FOUND, CallToolRequestParams

from primer.mcp.exposure import ExposureDeps, ToolNotExposable, update_exposure
from primer.mcp.server import build_mcp_server, current_actor
from primer.model.access import Access
from primer.model.mcp_exposure import McpExposure
from primer.model.principal import Principal
from primer.model.providers.toolset import Toolset, ToolsetProviderType
from tests.mcp.conftest import FakeProviderRegistry, FakeToolsetProvider, _make_tool

_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
SECRET = "ats__secret"
BUILTIN = "misc__uuid_v4"


def _user(user_id: str, role: str = "user") -> Principal:
    return Principal(type="user", id=user_id, display=user_id, role=role, source="local", user_id=user_id)


async def _private_toolset(storage):
    """User ``ua`` owns toolset ``ats`` (``ug`` holds a read grant, ``secret`` is a public tool) next to the built-in ``misc``."""
    await storage.get_storage(Toolset).create(Toolset(
        id="ats", provider=ToolsetProviderType.INTERNAL,
        access=Access(owners=["ua"], grants={"ug": "read"}, public_tools=["secret"]),
    ))
    ats = FakeToolsetProvider("ats", [_make_tool("ats", "secret", args_schema=_SCHEMA)], roles={"secret": "user"})
    misc = FakeToolsetProvider("misc", [_make_tool("misc", "uuid_v4")], roles={"uuid_v4": "user"})
    deps = ExposureDeps(storage_provider=storage, provider_registry=FakeProviderRegistry({"ats": ats, "misc": misc}))
    return deps, ats


async def _as(actor: Principal, server, method: str, params=None):
    token = current_actor.set(actor)
    try:
        return await server.get_request_handler(method).handler(None, params)
    finally:
        current_actor.reset(token)


@pytest.mark.asyncio
async def test_an_admin_cannot_expose_a_private_toolsets_tool(fake_storage_provider) -> None:
    deps, _ats = await _private_toolset(fake_storage_provider)

    with pytest.raises(ToolNotExposable) as info:
        await update_exposure(enabled=True, allowed_tools=[SECRET, BUILTIN], updated_by="admin", deps=deps)
    assert info.value.reason == "not_system_toolset"


@pytest.mark.parametrize("who", ["ub", "ua", "ug", "root"])
@pytest.mark.asyncio
async def test_an_allowlisted_private_tool_is_listed_and_called_by_nobody(fake_storage_provider, who) -> None:
    """Stranger, owner, read grantee and admin alike: the floor wins over a stored allowlist."""
    deps, ats = await _private_toolset(fake_storage_provider)
    await fake_storage_provider.get_storage(McpExposure).create(McpExposure(
        enabled=True, allowed_tools=[SECRET, BUILTIN, "ats__nope"], updated_at=datetime.now(timezone.utc),
    ))
    server = build_mcp_server(lambda: deps)
    actor = _user(who, role="admin" if who == "root" else "user")

    listed = await _as(actor, server, "tools/list")
    assert [tool.name for tool in listed.tools] == [BUILTIN]
    refusals = {}
    for name, arguments in ((SECRET, {"n": 1}), (SECRET, {}), ("ats__nope", {"n": 1})):  # {} fails the schema: no validation leak
        with pytest.raises(MCPError) as info:
            await _as(actor, server, "tools/call", CallToolRequestParams(name=name, arguments=arguments))
        assert info.value.code == METHOD_NOT_FOUND
        refusals[name] = info.value.message.replace(name, "<tool>")
    assert refusals[SECRET] == refusals["ats__nope"]
    assert ats.calls == []
    builtin = await _as(actor, server, "tools/call", CallToolRequestParams(name=BUILTIN, arguments={}))
    assert builtin.is_error is False
