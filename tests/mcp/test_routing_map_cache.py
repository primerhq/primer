"""hotpaths #3: build_routing_map memoizes on McpExposure.updated_at.

Proves the routing map is rebuilt only when the exposure stamp changes:
a second call with the same stamp reuses the cached map (no re-enumeration),
and an exposure mutation (which bumps updated_at) invalidates it.
"""

from __future__ import annotations

import pytest

from primer.mcp.exposure import (
    ExposureDeps,
    _clear_routing_cache,
    build_routing_map,
    get_exposure,
    update_exposure,
)
from primer.model.chat import Tool
from primer.model.provider import Toolset, ToolsetProviderType

from tests.mcp.conftest import FakeProviderRegistry, FakeToolsetProvider


def _tool(toolset_id: str, name: str) -> Tool:
    return Tool(
        id=name,
        toolset_id=toolset_id,
        description=f"{toolset_id}.{name}",
        args_schema={"type": "object", "properties": {}},
    )


class _CountingProvider(FakeToolsetProvider):
    """Wraps FakeToolsetProvider to count list_tools enumerations."""

    def __init__(self, toolset_id, tools):
        super().__init__(toolset_id, tools)
        self.enumerations = 0

    async def list_tools(self, *, principal=None):
        self.enumerations += 1
        async for t in super().list_tools(principal=principal):
            yield t


@pytest.fixture(autouse=True)
def _reset_cache():
    _clear_routing_cache()
    yield
    _clear_routing_cache()


async def _deps(fake_storage_provider):
    ts_store = fake_storage_provider.get_storage(Toolset)
    await ts_store.create(
        Toolset(id="acme__ts", provider=ToolsetProviderType.INTERNAL),
    )
    provider = _CountingProvider("acme__ts", [_tool("acme__ts", "hello")])
    registry = FakeProviderRegistry({"acme__ts": provider})
    deps = ExposureDeps(
        storage_provider=fake_storage_provider, provider_registry=registry,
    )
    return deps, provider


@pytest.mark.asyncio
async def test_second_call_same_stamp_uses_cache(fake_storage_provider):
    deps, provider = await _deps(fake_storage_provider)

    r1 = await build_routing_map(deps)
    enum_after_first = provider.enumerations
    assert r1["acme__ts__hello"] == ("acme__ts", "hello")
    assert enum_after_first >= 1

    r2 = await build_routing_map(deps)
    # Same exposure stamp -> served from cache, no new enumeration.
    assert provider.enumerations == enum_after_first
    assert r2 == r1


@pytest.mark.asyncio
async def test_exposure_mutation_invalidates_cache(fake_storage_provider):
    deps, provider = await _deps(fake_storage_provider)

    await build_routing_map(deps)
    enum_after_first = provider.enumerations

    # Mutating exposure bumps updated_at -> next build_routing_map rebuilds.
    await update_exposure(
        enabled=True, allowed_tools=None, updated_by="op", deps=deps,
    )
    await build_routing_map(deps)
    assert provider.enumerations > enum_after_first


@pytest.mark.asyncio
async def test_use_cache_false_always_rebuilds(fake_storage_provider):
    deps, provider = await _deps(fake_storage_provider)

    await build_routing_map(deps, use_cache=False)
    enum1 = provider.enumerations
    await build_routing_map(deps, use_cache=False)
    assert provider.enumerations > enum1
