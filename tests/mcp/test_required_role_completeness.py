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
from primer.toolset.canvas import build_canvas_toolset
from primer.toolset.collections import build_collections_toolset
from primer.toolset.harness import build_harness_toolset_provider
from primer.toolset.misc import build_misc_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.toolset.web import build_web_toolset
from primer.toolset.crud import build_crud_toolset
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

    collections_toolset = build_collections_toolset(
        storage_provider=fake_storage_provider,
        provider_registry=registry,
        semantic_search_registry=None,
    )
    registry._collections_toolset_provider = collections_toolset  # noqa: SLF001

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

    crud_toolset = build_crud_toolset(storage_provider=fake_storage_provider)
    registry._crud_toolset_provider = crud_toolset  # noqa: SLF001

    canvas_toolset = build_canvas_toolset(storage_provider=fake_storage_provider)
    registry._canvas_toolset_provider = canvas_toolset  # noqa: SLF001
    from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings

    fake_storage_provider.get_storage(CanvasSettings)._data[CANVAS_SETTINGS_ID] = CanvasSettings(  # noqa: SLF001
        id=CANVAS_SETTINGS_ID, agent_tools_enabled=True,
    )

    # search.py only needs a subsystem for its handlers (call()); list_tools()
    # never touches it, so a bare mock is a faithful stand-in here.

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


@pytest.mark.asyncio
async def test_call_tool_meta_dispatcher_never_exposable(
    reserved_provider_registry: ProviderRegistry,
) -> None:
    """Regression pin: ``system__call_tool`` must never pass ``is_exposable``.

    WHY THIS MATTERS (read before "fixing" this test): ``call_tool`` is
    role ``user`` (pivot P1), and its handler
    (``primer.toolset._system_crud._call_tool_tool``) dispatches the inner
    tool itself, NOT through ``primer.mcp.dispatch``'s ``invoke_exposed``
    path, which is where the MCP RBAC gate lives. The handler checks the
    INNER tool's ``required_role`` against the RUN's principal
    (``primer.agent.inner_dispatch``), read off the ``ToolContext``. The
    MCP endpoint hands handlers no context, so over MCP that check would
    rank every caller as an unattributed user rather than as the
    authenticated actor, and the approval gate and every pausing inner
    tool would have no session to park on.

    ``call_tool`` stays off MCP because it is declared ``yields=True`` (it
    parks for tool-approval, and a pausing inner tool pauses through it),
    which makes ``is_exposable`` reject it via the "yielding_unsupported"
    branch. If a future change drops ``yields=True`` (say, to make its
    non-approval-gated path exposable), this test starts failing. Do not
    just delete/relax this test to make it pass: first decide how an MCP
    call's actor reaches the inner role check and what a gated or pausing
    inner tool does with no park surface.
    """
    provider = await reserved_provider_registry.get_toolset("system")
    call_tool = None
    async for tool in provider.list_tools():
        if tool.id == "call_tool":
            call_tool = tool
            break
    assert call_tool is not None, "system toolset must still define call_tool"

    ok, reason = is_exposable(call_tool, provider=provider)

    assert ok is False, (
        "system__call_tool became exposable over MCP: its handler checks "
        "the inner tool's role against the run's context, which an MCP "
        "call does not carry; see this test's docstring before relaxing it"
    )
    assert reason == "yielding_unsupported"
