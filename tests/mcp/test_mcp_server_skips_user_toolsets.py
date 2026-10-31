"""W10: tools/list and tools/call never enumerate a user toolset.

User toolsets are never exposable (``safety.is_exposable``: ``not_system_toolset``), yet every tools/list,
every tools/call pre-check and every routing-map build walked them: one stdio MCP server that never answers
hung primer's own /v1/mcp. Inverted from the review probe ``test_user_toolset_enumerated_on_every_mcp_call``.
"""

from __future__ import annotations

import asyncio

from primer.mcp.dispatch import invoke_exposed, list_exposed_tools
from primer.mcp.exposure import ExposureDeps, _clear_routing_cache, get_exposure
from primer.mcp.server import _input_validation_error
from primer.model.chat import Tool
from primer.model.mcp_exposure import McpExposure
from primer.model.provider import Toolset, ToolsetProviderType
from tests.conftest import _FakeStorageProvider
from tests.mcp.conftest import FakeProviderRegistry, FakeToolsetProvider


class _HangingProvider(FakeToolsetProvider):
    calls = 0

    async def list_tools(self, *, principal=None):
        type(self).calls += 1
        await asyncio.sleep(3600)   # a stdio MCP server that never answers initialize
        if False:
            yield None


async def test_a_hanging_user_toolset_is_never_listed(system_actor) -> None:
    _clear_routing_cache()
    _HangingProvider.calls = 0
    sp = _FakeStorageProvider()
    await sp.get_storage(Toolset).create(Toolset(id="ext_mcp", provider=ToolsetProviderType.INTERNAL))
    misc = FakeToolsetProvider(
        "misc", [Tool(id="uuid_v4", toolset_id="misc", description="", args_schema={"type": "object", "properties": {}})],
        roles={"uuid_v4": "user"},
    )
    deps = ExposureDeps(
        storage_provider=sp,
        provider_registry=FakeProviderRegistry({"misc": misc, "ext_mcp": _HangingProvider("ext_mcp", [])}),
    )
    row = await get_exposure(deps)
    await sp.get_storage(McpExposure).update(row.model_copy(update={"enabled": True, "allowed_tools": ["misc__uuid_v4"]}))

    assert await asyncio.wait_for(_input_validation_error(deps, "misc__uuid_v4", {}), 1) is None
    assert len(await asyncio.wait_for(list_exposed_tools(deps), 1)) == 1
    result = await asyncio.wait_for(
        invoke_exposed(scoped_id="misc__uuid_v4", arguments={}, principal="p", deps=deps, actor=system_actor), 1,
    )
    assert result.is_error is False
    assert _HangingProvider.calls == 0
