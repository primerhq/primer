"""Unit tests for the ``workspace_ext`` reserved toolset.

``workspace_ext`` groups the workspace-session-only yielding tools moved
out of misc / workspaces / trigger:

* ``sleep``            (from misc)
* ``watch_files``      (from workspaces)
* ``invoke_graph``     (from workspaces)
* ``subscribe_to_trigger`` (from trigger)

The BARE tool ids are unchanged by the move - only the scoped id
(``toolset_id__bare``) changes. These tests assert the new toolset
assembly, the preserved capability flags, and that the bare-name-keyed
resume hooks are still registered.
"""

from __future__ import annotations

import pytest

from primer.toolset.workspace_ext import (
    WORKSPACE_EXT_TOOLSET_ID,
    build_workspace_ext_toolset,
)


class _SP:
    """Minimal storage_provider stub.

    Only ``subscribe_to_trigger`` touches storage and these tests never
    dispatch it, so a no-op ``get_storage`` suffices.
    """

    def get_storage(self, model):  # pragma: no cover - never dispatched here
        return None


def _provider():
    return build_workspace_ext_toolset(storage_provider=_SP())


@pytest.mark.asyncio
async def test_workspace_ext_tool_ids():
    provider = _provider()
    names = {t.id async for t in provider.list_tools()}
    assert names == {
        "sleep",
        "watch_files",
        "invoke_graph",
        "subscribe_to_trigger",
        "subscribe_to_channel_event",
    }


@pytest.mark.asyncio
async def test_workspace_ext_tools_carry_toolset_id():
    provider = _provider()
    async for tool in provider.list_tools():
        assert tool.toolset_id == WORKSPACE_EXT_TOOLSET_ID


def test_all_four_tools_yield():
    provider = _provider()
    for name in (
        "sleep",
        "watch_files",
        "invoke_graph",
        "subscribe_to_trigger",
        "subscribe_to_channel_event",
    ):
        assert provider.is_yielding(name) is True, name


@pytest.mark.asyncio
async def test_scoped_ids_via_tool_manager():
    """Through the manager the tools surface as workspace_ext__<bare>."""
    from primer.agent.tool_manager import ToolExecutionManager

    provider = _provider()

    class _Sess:
        workspace_id = "ws-1"
        session_id = "sess-1"
        agent_id = "agent-1"
        workspace_tools: list = []

    mgr = ToolExecutionManager(
        toolset_providers={WORKSPACE_EXT_TOOLSET_ID: provider},  # type: ignore[arg-type]
        workspace_session=_Sess(),  # type: ignore[arg-type]
    )
    scoped = {t.id for t in await mgr.list_tools()}
    assert scoped == {
        "workspace_ext__sleep",
        "workspace_ext__watch_files",
        "workspace_ext__invoke_graph",
        "workspace_ext__subscribe_to_trigger",
        "workspace_ext__subscribe_to_channel_event",
    }


def test_resume_hooks_registered_by_bare_name():
    # Importing the source modules registers the resume hooks keyed on the
    # unchanged BARE names. The move between toolsets must not alter them.
    import primer.toolset.misc  # noqa: F401  (registers sleep)
    import primer.toolset.workspaces  # noqa: F401  (registers watch_files)
    from primer.worker.yield_resume_registry import get_resume_hook

    assert callable(get_resume_hook("sleep"))
    assert callable(get_resume_hook("watch_files"))


@pytest.mark.asyncio
async def test_workspace_ext_in_reserved_ids():
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS

    assert WORKSPACE_EXT_TOOLSET_ID in RESERVED_TOOLSET_IDS


@pytest.mark.asyncio
async def test_workspace_ext_tools_not_exposable_over_mcp():
    """All four are yielding, so is_exposable denies them on MCP."""
    from primer.mcp.safety import is_exposable

    provider = _provider()
    async for tool in provider.list_tools():
        ok, reason = is_exposable(tool, provider=provider)
        assert ok is False
        assert reason == "yielding_unsupported"
