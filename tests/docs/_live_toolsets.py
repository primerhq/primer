"""The built-in toolsets, built once with test fakes, for the docs-vs-registry checks.

Built with every optional collaborator wired (the real app wires them): a tool that exists only then, such as ``web::download``, which needs a
workspace registry, is a real tool. A name is stale only if no wiring has it.

``built_in_providers()`` maps ``toolset id -> provider`` for all ten curated toolsets (``collections`` included: a
check that left one out would pass for every document that names its tools; ``canvas`` joined with pivot P5, design 8.3).
``reserved_tools()`` (M3) is the whole live catalogue the docs may cite: the curated ten, every generated REST-backed
provider (wired production-shaped through ``create_test_app``, the docs-vs-registry checks run against that live
catalogue) and the session and client scopes (``collect_tool_entries`` adds them, as the boot does). ``built_in_tools()``
maps ``toolset id -> {spelling -> Tool}`` and derives from it; ``reserved_tools()`` derives the same for the catalogue.
A tool is reachable under every spelling the docs use: its registered (bare) id and the catalog's scoped id (a call's
``tool_name`` is the bare id, ``list``; the docs name the tool by its scoped id, ``harness__list``).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from unittest.mock import MagicMock

from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool


@lru_cache(maxsize=1)
def built_in_providers() -> dict[str, ToolsetProvider]:
    """The ten curated toolsets' providers, keyed by toolset id. Shared and cached: read them, do not change them."""
    from primer.api.registries import ProviderRegistry
    from primer.toolset.canvas import build_canvas_toolset
    from primer.toolset.collections import build_collections_toolset
    from primer.toolset.crud import build_crud_toolset
    from primer.toolset.harness import build_harness_toolset_provider
    from primer.toolset.misc import build_misc_toolset
    from primer.toolset.system import build_system_toolset
    from primer.toolset.trigger import build_trigger_toolset_provider
    from primer.toolset.web import build_web_toolset
    from primer.toolset.workspace_ext import build_workspace_ext_toolset
    from primer.toolset.workspaces import build_workspaces_toolset
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    registry = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    providers = {
        "system": build_system_toolset(
            storage_provider=sp,
            provider_registry=registry,
            semantic_search_registry=MagicMock(),
            workspace_registry=MagicMock(),
        ),
        "crud": build_crud_toolset(storage_provider=sp),
        "collections": build_collections_toolset(
            storage_provider=sp, provider_registry=registry, semantic_search_registry=MagicMock(),
        ),
        "harness": build_harness_toolset_provider(storage_provider=sp),
        "trigger": build_trigger_toolset_provider(storage_provider=sp),
        "workspaces": build_workspaces_toolset(storage_provider=sp, workspace_registry=MagicMock()),
        "workspace_ext": build_workspace_ext_toolset(storage_provider=sp),
        "web": build_web_toolset(
            web_search_service=MagicMock(), web_fetch_service=MagicMock(), workspace_registry=MagicMock(),
        ),
        "misc": build_misc_toolset(),
        "canvas": build_canvas_toolset(storage_provider=sp),
    }
    return providers


@lru_cache(maxsize=1)
def built_in_tools() -> dict[str, dict[str, Tool]]:
    providers = built_in_providers()

    async def collect() -> dict[str, dict[str, Tool]]:
        built: dict[str, dict[str, Tool]] = {}
        for toolset_id, provider in providers.items():
            spellings: dict[str, Tool] = {}
            async for tool in provider.list_tools():
                spellings[tool.id] = tool
                spellings[f"{toolset_id}__{tool.id}"] = tool
            built[toolset_id] = spellings
        return built

    return asyncio.run(collect())


@lru_cache(maxsize=1)
def reserved_tools() -> dict[str, dict[str, Tool]]:
    """``toolset id -> {spelling -> Tool}`` for the whole live tool catalogue the docs may cite (M3):
    the ten curated toolsets, every generated REST-backed provider, and the session and client scopes.

    The generated half is wired production-shaped: ``create_test_app`` compiles the per-resource route
    toolsets from the mounted routers onto a real ``ProviderRegistry`` (the same flow the production
    build and lifespan run), and ``collect_tool_entries`` adds the session and client scopes exactly
    as the boot does. So a doc that cites a generated tool (``agents__get_access``) or a scope tool
    (``workspace__read``) is checked like any other. A stale citation is a red suite (W1-08b), not a
    runbook typo. Cached like ``built_in_tools``: read them, do not change them.
    """
    from primer.api.app import create_test_app
    from primer.api.registries import ProviderRegistry
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
    from primer.toolset.tool_catalog import collect_tool_entries
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    registry = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda p: object(),
    )
    create_test_app(storage_provider=sp, provider_registry=registry)

    async def collect() -> dict[str, dict[str, Tool]]:
        providers = dict(built_in_providers())  # the curated ten, built with the fixture's fakes
        for toolset_id in sorted(RESERVED_TOOLSET_IDS):
            if toolset_id not in providers:
                providers[toolset_id] = await registry.get_toolset(toolset_id)
        built: dict[str, dict[str, Tool]] = {}
        for entry in await collect_tool_entries(providers):
            spellings = built.setdefault(entry.toolset_id, {})
            spellings[entry.tool.id] = entry.tool
            spellings[f"{entry.toolset_id}__{entry.tool.id}"] = entry.tool
        return built

    return asyncio.run(collect())
