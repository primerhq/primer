"""The built-in toolsets, built once with test fakes, for the docs-vs-registry checks.

Built with every optional collaborator wired (the real app wires them): a tool that exists only then, such as ``web::download``, which needs a
workspace registry, is a real tool. A name is stale only if no wiring has it.

``built_in_tools()`` maps ``toolset id -> {spelling -> Tool}``. A tool is reachable under every spelling the docs use: its registered id, and
that id without the ``<toolset>__`` prefix (the harness toolset registers ``harness__list`` and the agent docs call it ``harness::harness__list``
while the dev docs write the scoped id ``harness__list``).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from unittest.mock import MagicMock

from primer.model.chat import Tool


@lru_cache(maxsize=1)
def built_in_tools() -> dict[str, dict[str, Tool]]:
    from primer.api.registries import ProviderRegistry
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
        "harness": build_harness_toolset_provider(storage_provider=sp),
        "trigger": build_trigger_toolset_provider(storage_provider=sp),
        "workspaces": build_workspaces_toolset(storage_provider=sp, workspace_registry=MagicMock()),
        "workspace_ext": build_workspace_ext_toolset(storage_provider=sp),
        "web": build_web_toolset(
            web_search_service=MagicMock(), web_fetch_service=MagicMock(), workspace_registry=MagicMock(),
        ),
        "misc": build_misc_toolset(),
    }

    async def collect() -> dict[str, dict[str, Tool]]:
        built: dict[str, dict[str, Tool]] = {}
        for toolset_id, provider in providers.items():
            spellings: dict[str, Tool] = {}
            async for tool in provider.list_tools():
                spellings[tool.id] = tool
                spellings[tool.id.removeprefix(f"{toolset_id}__")] = tool
            built[toolset_id] = spellings
        return built

    return asyncio.run(collect())
