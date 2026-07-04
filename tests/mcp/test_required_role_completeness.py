"""Every exposable reserved tool must DECLARE a role — the fail-closed
'admin' default (primer.toolset.internal.InternalToolsetProvider.required_role)
is only a safety net for tools nobody has classified yet, never something
this codebase relies on for an actual authorization decision.

The fixture below builds the SAME built-in providers the MCP endpoint
enumerates: primer.mcp.exposure._iter_catalogue loops
``primer.api.registries.provider_registry.RESERVED_TOOLSET_IDS`` and
resolves each id via ``ProviderRegistry.get_toolset``. We construct a real
``ProviderRegistry`` and wire each reserved builder onto it exactly the way
``primer/api/_app_lifespan.py`` does (assigning the private
``_<name>_toolset_provider`` attributes — the only way the registry's
constructor-time collaborators can be supplied post-construction; the
lifespan does the same, e.g. ``provider_registry._system_toolset_provider =
system_toolset``). Resolving through ``RESERVED_TOOLSET_IDS`` +
``registry.get_toolset(...)`` (rather than a hand-written id list) means a
new reserved toolset can't silently fall outside this test's coverage.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.mcp.safety import is_exposable, tool_scoped_id
from primer.toolset.harness import build_harness_toolset_provider
from primer.toolset.misc import build_misc_toolset
from primer.toolset.search import build_search_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.toolset.web import build_web_toolset
from primer.toolset.workspace_ext import build_workspace_ext_toolset
from primer.toolset.workspaces import build_workspaces_toolset


@pytest.fixture
def reserved_provider_registry(fake_storage_provider) -> ProviderRegistry:
    """A real ``ProviderRegistry`` with every reserved built-in toolset wired
    on, mirroring ``primer/api/_app_lifespan.py`` (construction order and the
    post-construction ``_<x>_toolset_provider`` assignment are identical)."""
    registry = ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )

    system_toolset = build_system_toolset(
        storage_provider=fake_storage_provider,
        provider_registry=registry,
    )
    registry._system_toolset_provider = system_toolset  # noqa: SLF001

    misc_toolset = build_misc_toolset()
    registry._misc_toolset_provider = misc_toolset  # noqa: SLF001

    web_toolset = build_web_toolset(
        # list_tools() never calls either service; only call() would.
        web_search_service=object(),  # type: ignore[arg-type]
        web_fetch_service=object(),  # type: ignore[arg-type]
    )
    registry._web_toolset_provider = web_toolset  # noqa: SLF001

    workspaces_toolset = build_workspaces_toolset(
        storage_provider=fake_storage_provider,
        workspace_registry=WorkspaceRegistry(fake_storage_provider),
    )
    registry._workspaces_toolset_provider = workspaces_toolset  # noqa: SLF001

    harness_toolset = build_harness_toolset_provider(
        storage_provider=fake_storage_provider,
    )
    registry._harness_toolset_provider = harness_toolset  # noqa: SLF001

    trigger_toolset = build_trigger_toolset_provider(
        storage_provider=fake_storage_provider,
    )
    registry._trigger_toolset_provider = trigger_toolset  # noqa: SLF001

    workspace_ext_toolset = build_workspace_ext_toolset(
        storage_provider=fake_storage_provider,
    )
    registry._workspace_ext_toolset_provider = workspace_ext_toolset  # noqa: SLF001

    # search.py only needs a subsystem for its handlers (call()); list_tools()
    # never touches it, so a bare mock is a faithful stand-in here.
    search_toolset = build_search_toolset(MagicMock())
    registry._search_toolset_provider = search_toolset  # noqa: SLF001

    return registry


@pytest.mark.asyncio
async def test_every_exposable_reserved_tool_declares_a_role(
    reserved_provider_registry: ProviderRegistry,
) -> None:
    missing: list[str] = []
    for toolset_id in RESERVED_TOOLSET_IDS:
        provider = await reserved_provider_registry.get_toolset(toolset_id)
        async for tool in provider.list_tools():
            ok, _reason = is_exposable(tool, provider=provider)
            if ok and tool.required_role is None:
                missing.append(tool_scoped_id(tool))
    assert not missing, (
        "exposable tools without an explicit required_role: "
        f"{sorted(missing)}"
    )
