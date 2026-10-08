"""The workspaces toolset has ONE rule for "is the caller an admin" (ticket 01a11b12, follow-up of #473).

Two predicates had grown in ``primer/toolset/workspaces.py``. ``_caller_is_admin`` (A-22, the reserved ``.state`` / ``.tmp`` reads) answered
"no" whenever there was no ToolContext, so an admin over MCP, whose calls are dispatched without one, was refused reads of the runtime's own
trees. ``_tool_caller_is_admin`` (the admin-only template fields, #473) fell back to the MCP request's actor, which is right, but ALSO fell back
when a ToolContext was present and carried no ``initiated_by``, so a task spawned from an MCP request could inherit that request's admin.

One rule: a run's ``initiated_by`` when there is a ToolContext (and nothing else: a context with no initiator is an unknown caller, not the MCP
request's), the MCP request's actor only when there is NO context at all, and neither means not admin. The floor is ``_role_allows``, which
since A-20 refuses ``trigger``-typed actors unless their role is high enough; only ``system`` always passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.mcp.server import current_actor
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.workspace import WorkspaceTemplate
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspaces import build_workspaces_toolset
from tests.toolset.test_workspaces import _LiveWorkspace, _StubBackend
from tests.toolset.test_workspaces_template_privilege import _PRIVILEGED, _call, _ctx

WS_ID = "ws-1"
STATE_FILE = ".state/sessions/s-1/session.json"


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    registry = WorkspaceRegistry(sp, factory=_StubBackend)
    live = _LiveWorkspace(WS_ID)
    live._files[STATE_FILE] = b'{"id": "s-1"}'
    live._files["src/a.txt"] = b"hello"

    async def get_workspace(workspace_id: str):
        return live

    registry.get_workspace = get_workspace  # type: ignore[method-assign]
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=registry)
    yield sp, toolset
    await sp.aclose()


def _actor(role: str, kind: str = "user") -> Principal:
    return Principal(type=kind, id=f"{kind}-{role}", display=role, role=role, source="local")


class as_mcp:
    """A tool call over MCP: no ToolContext, the request's actor on ``current_actor``."""

    def __init__(self, role: str, kind: str = "user") -> None:
        self._actor = _actor(role, kind)

    def __enter__(self):
        self._token = current_actor.set(self._actor)

    def __exit__(self, *exc):
        current_actor.reset(self._token)


def _no_initiator_ctx() -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id="sess-1", workspace_id=None, initiated_by=None)


async def _read_state(toolset, *, ctx: ToolContext | None = None):
    return await _call(toolset, "read_workspace_file", ctx=ctx, workspace_id=WS_ID, path=STATE_FILE)


async def _list_state(toolset, *, ctx: ToolContext | None = None):
    return await _call(toolset, "list_workspace_files", ctx=ctx, workspace_id=WS_ID, path=".state")


async def _hold_an_admin_only_template(toolset) -> None:
    assert not (await _call(toolset, "create_workspace_template", ctx=_ctx("admin"), entity=_PRIVILEGED["files.secret"]))[0]


# ---- A-22: an admin over MCP may read the runtime's own trees ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_mcp_admin_reads_a_file_in_the_reserved_state_tree(world) -> None:
    _, toolset = world
    with as_mcp("admin"):
        is_error, answer = await _read_state(toolset)

    assert not is_error, f"an admin over MCP was refused the runtime's .state tree: {answer}"


@pytest.mark.asyncio
async def test_an_mcp_admin_lists_the_reserved_state_tree(world) -> None:
    _, toolset = world
    with as_mcp("admin"):
        is_error, answer = await _list_state(toolset)

    assert not is_error, f"an admin over MCP was refused a listing of the runtime's .state tree: {answer}"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "restricted"])
async def test_a_non_admin_mcp_caller_is_refused_the_reserved_state_tree(world, role) -> None:
    _, toolset = world
    with as_mcp(role):
        read_error, read_answer = await _read_state(toolset)
        list_error, list_answer = await _list_state(toolset)

    assert read_error and read_answer["type"] == "forbidden", read_answer
    assert list_error and list_answer["type"] == "forbidden", list_answer


@pytest.mark.asyncio
async def test_a_call_with_no_known_caller_is_refused_the_reserved_state_tree(world) -> None:
    _, toolset = world

    is_error, answer = await _read_state(toolset)

    assert is_error and answer["type"] == "forbidden", answer


@pytest.mark.asyncio
async def test_an_admin_run_still_reads_the_reserved_state_tree(world) -> None:
    _, toolset = world

    is_error, answer = await _read_state(toolset, ctx=_ctx("admin"))

    assert not is_error, answer


@pytest.mark.asyncio
async def test_a_trigger_run_of_a_user_is_refused_the_reserved_state_tree(world) -> None:
    """A-20: a ``trigger``-typed actor is ranked by its role like any other (it used to be waved through), so a role=user trigger run is not an admin."""
    _, toolset = world

    is_error, answer = await _read_state(toolset, ctx=_ctx("user", kind="trigger"))

    assert is_error and answer["type"] == "forbidden", answer


# ---- #473: an admin over MCP updates and deletes a template that holds admin-only settings ---------------------------------------------------


@pytest.mark.asyncio
async def test_an_mcp_admin_updates_a_template_that_holds_admin_only_settings(world) -> None:
    sp, toolset = world
    await _hold_an_admin_only_template(toolset)
    changed = {**_PRIVILEGED["files.secret"], "description": "changed by an MCP admin"}

    with as_mcp("admin"):
        is_error, answer = await _call(toolset, "update_workspace_template", id="tpl-s", entity=changed)

    assert not is_error, answer
    assert (await sp.get_storage(WorkspaceTemplate).get("tpl-s")).description == "changed by an MCP admin"


@pytest.mark.asyncio
async def test_an_mcp_admin_deletes_a_template_that_holds_admin_only_settings(world) -> None:
    sp, toolset = world
    await _hold_an_admin_only_template(toolset)

    with as_mcp("admin"):
        is_error, answer = await _call(toolset, "delete_workspace_template", id="tpl-s")

    assert not is_error, answer
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-s") is None


@pytest.mark.asyncio
async def test_a_non_admin_mcp_caller_cannot_update_or_delete_a_template_that_holds_admin_only_settings(world) -> None:
    sp, toolset = world
    await _hold_an_admin_only_template(toolset)
    changed = {**_PRIVILEGED["files.secret"], "description": "changed by an MCP user"}

    with as_mcp("user"):
        update_error, update_answer = await _call(toolset, "update_workspace_template", id="tpl-s", entity=changed)
        delete_error, delete_answer = await _call(toolset, "delete_workspace_template", id="tpl-s")

    assert update_error and update_answer["type"] == "forbidden", update_answer
    assert delete_error and delete_answer["type"] == "forbidden", delete_answer
    stored = await sp.get_storage(WorkspaceTemplate).get("tpl-s")
    assert stored is not None and stored.description == "d"


# ---- a ToolContext with no initiator is an unknown caller, not the MCP request's ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_context_with_no_initiator_does_not_inherit_the_mcp_requests_actor_for_templates(world) -> None:
    """A task spawned from an MCP admin's request runs with a ToolContext and (here) no initiator, while the spawning request's actor is
    still on the context variable. It must not be an admin because of that."""
    sp, toolset = world
    await _hold_an_admin_only_template(toolset)
    changed = {**_PRIVILEGED["files.secret"], "description": "changed by a task"}

    with as_mcp("admin"):
        update_error, update_answer = await _call(
            toolset, "update_workspace_template", ctx=_no_initiator_ctx(), id="tpl-s", entity=changed,
        )
        delete_error, delete_answer = await _call(toolset, "delete_workspace_template", ctx=_no_initiator_ctx(), id="tpl-s")

    assert update_error and update_answer["type"] == "forbidden", update_answer
    assert delete_error and delete_answer["type"] == "forbidden", delete_answer
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-s") is not None


@pytest.mark.asyncio
async def test_a_tool_context_with_no_initiator_does_not_inherit_the_mcp_requests_actor_for_reads(world) -> None:
    _, toolset = world
    with as_mcp("admin"):
        is_error, answer = await _read_state(toolset, ctx=_no_initiator_ctx())

    assert is_error and answer["type"] == "forbidden", answer
