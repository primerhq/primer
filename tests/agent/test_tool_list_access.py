"""The tool list a run offers is filtered by the run's person (access-control P4 Task 2, spec 6.9).

A toolset row the person holds no level on is left out and noticed (``not available to you``); a row with public tools offers exactly those
tools (``some tools not available to you``); a remote (MCP) toolset the person may not use is never contacted; a sub-agent's catalogue is its
parent run's person's; and a graph ToolCall manager with no identity is an unattributed user, never ``system``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext.model_rebuild, as in the other tool-manager tests)
from primer.agent import tool_listing
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.access import Access
from primer.model.chat import Tool, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig, Toolset, ToolsetProviderType
from primer.storage.sqlite import SqliteStorageProvider

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
USER = PrincipalRef(type="user", id="u-user", display="user", role="user", source="local", user_id="u-user")
OWNER = PrincipalRef(type="user", id="u-owner", display="owner", role="user", source="local", user_id="u-owner")
ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local", user_id="u-admin")


def _tool(name: str, toolset_id: str) -> Tool:
    return Tool(id=name, description=f"the tool {name}", toolset_id=toolset_id, args_schema=_SCHEMA)


class _Provider:
    def __init__(self, toolset_id: str, *names: str, remote: bool = False) -> None:
        self.tools = [_tool(n, toolset_id) for n in names]
        self.lists_remotely = remote
        self.listings = 0

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        self.listings += 1
        for tool in self.tools:
            yield tool

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        return ToolCallResult(output=f"{tool_name}()", is_error=False)


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    store = provider.get_storage(Toolset)
    for tid, access in {
        "private": Access(owners=["u-owner"]),
        "shared": Access(owners=["u-owner"], public=True),
        "one-tool": Access(owners=["u-owner"], public_tools=["t1"]),
        "remote": Access(owners=["u-owner"]),
    }.items():
        await store.create(Toolset(id=tid, provider=ToolsetProviderType.INTERNAL, access=access))
    try:
        yield provider
    finally:
        await provider.aclose()


def _manager(sp, providers: dict, who: PrincipalRef | None, **kw) -> ToolExecutionManager:
    return ToolExecutionManager(
        toolset_providers=providers, initiated_by=who, provider_registry=SimpleNamespace(storage_provider=sp), **kw,
    )


async def test_unreadable_toolset_is_left_out_and_noticed(sp) -> None:
    private = _Provider("private", "s")
    manager = _manager(sp, {"shared": _Provider("shared", "a"), "private": private, "builtin": _Provider("builtin", "b")}, USER)

    catalogue = await manager.list_tools()

    assert sorted(t.id for t in catalogue) == ["builtin__b", "shared__a"]
    assert manager.unavailable_toolsets() == {"private": tool_listing.NO_ACCESS}
    assert tool_listing.unavailable_toolsets_notice(manager.unavailable_toolsets()) == (
        f"{tool_listing.NOTICE_HEADER}\n- private: not available to you"
    )
    assert private.listings == 0, "a toolset the person may not use is never listed"
    assert "private__s" not in manager._tool_to_toolset  # noqa: SLF001 - not reachable by any door


async def test_only_public_tools_are_offered(sp) -> None:
    manager = _manager(sp, {"one-tool": _Provider("one-tool", "t1", "t2")}, USER)

    catalogue = await manager.list_tools()

    assert [t.id for t in catalogue] == ["one-tool__t1"]
    assert manager.unavailable_toolsets() == {"one-tool": tool_listing.SOME_TOOLS}
    assert "one-tool__t2" not in manager._tool_to_toolset  # noqa: SLF001 - the sibling is reachable by no door


async def test_the_owner_and_an_admin_get_every_tool_and_no_notice(sp) -> None:
    for who in (OWNER, ADMIN):
        manager = _manager(sp, {"one-tool": _Provider("one-tool", "t1", "t2"), "private": _Provider("private", "s")}, who)
        assert sorted(t.id for t in await manager.list_tools()) == ["one-tool__t1", "one-tool__t2", "private__s"]
        assert manager.unavailable_toolsets() == {}


async def test_unreadable_mcp_toolset_is_not_listed(sp) -> None:
    remote = _Provider("remote", "x", remote=True)
    manager = _manager(sp, {"remote": remote}, USER)

    assert await manager.list_tools() == []

    assert remote.listings == 0, "an unreadable remote toolset is never contacted (no connection, no sign-in prompt)"
    assert manager.unavailable_toolsets() == {"remote": tool_listing.NO_ACCESS}


async def test_subagent_catalogue_is_the_parents_persons(sp) -> None:
    from primer.agent.invoke import build_subagent_toolmanager

    class _Registry:
        storage_provider = sp

        async def get_toolset(self, tid: str):
            return {"private": _Provider("private", "s"), "shared": _Provider("shared", "a")}[tid]

    def _context(who: PrincipalRef) -> SimpleNamespace:
        return SimpleNamespace(
            session_id=None, workspace_id=None, chat_id="c1", tools=["private__s", "shared__a"], initiated_by=who, turn_no=None,
        )

    for who, expected in ((USER, ["shared__a"]), (OWNER, ["private__s", "shared__a"])):
        manager = await build_subagent_toolmanager(_context(who), provider_registry=_Registry())
        assert sorted(t.id for t in await manager.list_tools()) == expected


async def test_graph_toolcall_without_identity_is_not_system(sp) -> None:
    from primer.graph.workspace_executor import WorkspaceGraphExecutor

    executor = WorkspaceGraphExecutor.__new__(WorkspaceGraphExecutor)
    executor._toolset_resolver = None  # noqa: SLF001
    executor._tool_manager = None  # noqa: SLF001
    executor._workspace_session = SimpleNamespace(session_id="s1", workspace_id="w1", agent_id=None, workspace_tools={})  # noqa: SLF001
    executor._approval_resolver = None  # noqa: SLF001
    executor._identity = None  # noqa: SLF001
    executor._graph = SimpleNamespace(id="g1")  # noqa: SLF001  (the ToolCall manager acts as its graph, P4 T1)
    executor._provider_registry = SimpleNamespace(storage_provider=sp)  # noqa: SLF001

    manager = await executor._manager_for(SimpleNamespace(tool_id="workspace__read"))  # noqa: SLF001

    assert manager._initiated_by is not None  # noqa: SLF001
    assert manager._initiated_by.type != "system" and manager._initiated_by.role != "admin"  # noqa: SLF001
