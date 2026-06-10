"""MCP dispatch resolution must use the precomputed routing map, not a
string split, so scoped ids resolve correctly regardless of how many ``__``
separators they contain.

Two pathological cases a naive split mis-handles:

* A harness-deployed toolset whose id itself contains ``__`` has a scoped
  tool id of ``<slug>__<template>__<bare>`` (e.g. ``acme__ts__hello``). A
  first-``__`` split yields toolset ``acme`` (WRONG; the row is ``acme__ts``).
* The built-in ``harness`` toolset whose OWN bare tool ids contain ``__``
  (e.g. ``harness__list``) has scoped id ``harness__harness__list``. A
  last-``__`` split yields bare ``list`` (WRONG; the bare name is
  ``harness__list``).

The routing map keys are the exact scoped ids the catalogue advertises, so
its inverse handles both.
"""

from __future__ import annotations

import pytest

from primer.mcp.exposure import ExposureDeps, build_routing_map
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


@pytest.mark.asyncio
async def test_routing_map_resolves_harness_deployed_toolset_with_dunder_id(
    fake_storage_provider,
) -> None:
    """A harness-deployed toolset id containing ``__`` resolves exactly.

    ``acme__ts__hello`` -> toolset ``acme__ts`` + tool ``hello`` (a
    first-``__`` split would mis-resolve to toolset ``acme``).
    """
    # The user-toolset branch of _iter_catalogue pages Toolset rows from
    # storage and resolves each via registry.get_toolset(row.id).
    ts_store = fake_storage_provider.get_storage(Toolset)
    await ts_store.create(
        Toolset(id="acme__ts", provider=ToolsetProviderType.INTERNAL),
    )
    provider = FakeToolsetProvider("acme__ts", [_tool("acme__ts", "hello")])
    registry = FakeProviderRegistry({"acme__ts": provider})
    deps = ExposureDeps(
        storage_provider=fake_storage_provider, provider_registry=registry,
    )

    routing = await build_routing_map(deps)

    assert routing["acme__ts__hello"] == ("acme__ts", "hello")


@pytest.mark.asyncio
async def test_routing_map_resolves_builtin_harness_dunder_bare_name(
    fake_storage_provider,
) -> None:
    """The built-in ``harness`` toolset's ``harness__list`` bare name resolves.

    ``harness__harness__list`` -> toolset ``harness`` + tool
    ``harness__list`` (a last-``__`` split would mis-resolve the bare name
    to ``list``).
    """
    # ``harness`` is a RESERVED toolset id so _iter_catalogue resolves it
    # from the registry without a storage row.
    provider = FakeToolsetProvider(
        "harness", [_tool("harness", "harness__list")],
    )
    registry = FakeProviderRegistry({"harness": provider})
    deps = ExposureDeps(
        storage_provider=fake_storage_provider, provider_registry=registry,
    )

    routing = await build_routing_map(deps)

    assert routing["harness__harness__list"] == ("harness", "harness__list")
