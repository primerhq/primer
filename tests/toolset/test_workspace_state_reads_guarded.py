"""A-22, the tool half: ``read_workspace_file``, ``get_workspace_file_info``
and ``list_workspace_files`` (``required_role="user"``) do not serve the
runtime's ``.state`` / ``.tmp`` trees unless the CALLER is an admin.

The caller is judged by ONE predicate (``_caller_is_admin``, ticket 01a11b12) under the floor's rule (``primer.authz._role_allows``): a call
with a ToolContext by that run's ``initiated_by`` alone (no initiator is an unknown caller, not the MCP request's), a call without one (the
MCP endpoint dispatches without one) by the request's actor, and with neither a reserved path fails closed. Paths are normalised before
the check.
"""

from __future__ import annotations

import json

import pytest

from primer.toolset.workspaces import build_workspaces_toolset
from tests._support.caller import caller
from tests.api.test_workspace_state_reads_guarded import (
    RESERVED_SPELLINGS,
    _Registry,
)
from tests.tap.test_mcp_tap_tool import _Provider
from tests.toolset.test_workspaces_admin_predicate import _no_initiator_ctx, as_mcp


def _toolset():
    registry = _Registry()
    ts = build_workspaces_toolset(
        storage_provider=_Provider(), workspace_registry=registry,
        tap_router=None,
    )
    return ts, registry


async def _call(ts, tool: str, arguments: dict, ctx):
    return await ts.call(
        tool_name=tool, arguments=arguments, principal=None, ctx=ctx,
    )


def _type(result) -> str | None:
    return json.loads(result.output).get("type") if result.is_error else None


@pytest.mark.parametrize("path", RESERVED_SPELLINGS)
@pytest.mark.parametrize(
    "tool", ["read_workspace_file", "get_workspace_file_info"],
)
@pytest.mark.parametrize("ctx", [caller("user"), None], ids=["user", "no-identity"])
async def test_a_non_admin_cannot_read_a_reserved_path(tool, path, ctx):
    ts, registry = _toolset()
    result = await _call(ts, tool, {"workspace_id": "w", "path": path}, ctx)
    assert result.is_error and _type(result) == "forbidden", result.output
    assert "Traceback" not in result.output
    assert registry.ws.reads == []


@pytest.mark.parametrize("path", [".state", "./.state/sessions", "a/../.tmp"])
async def test_a_user_cannot_list_inside_a_reserved_tree(path):
    ts, _ = _toolset()
    result = await _call(
        ts, "list_workspace_files", {"workspace_id": "w", "path": path},
        caller("user"),
    )
    assert result.is_error and _type(result) == "forbidden", result.output


async def test_a_recursive_listing_drops_reserved_entries_for_a_user():
    ts, _ = _toolset()
    result = await _call(
        ts, "list_workspace_files",
        {"workspace_id": "w", "path": ".", "recursive": True}, caller("user"),
    )
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert [i["path"] for i in body["items"]] == ["src", "src/main.py"]
    assert body["total"] == 2


async def test_a_user_still_reads_their_own_files():
    ts, _ = _toolset()
    result = await _call(
        ts, "read_workspace_file", {"workspace_id": "w", "path": "src/main.py"},
        caller("user"),
    )
    assert not result.is_error, result.output
    assert json.loads(result.output)["content"] == "hello\n"


@pytest.mark.parametrize("ctx", [caller("admin"), caller(None, kind="system")], ids=["admin", "system"])
async def test_an_admin_or_internal_actor_may_read_reserved_paths(ctx):
    ts, _ = _toolset()
    result = await _call(
        ts, "read_workspace_file",
        {"workspace_id": "w", "path": "./.state/sessions/s1/messages.jsonl"}, ctx,
    )
    assert not result.is_error, result.output
    assert "Traceback" in json.loads(result.output)["content"]


# ---- the MCP endpoint (no ToolContext): the request's actor decides (ticket 01a11b12) -----------------------------------------------------

RESERVED_FILE = "./.state/sessions/s1/messages.jsonl"
ALL_ENTRIES = ["src", "src/main.py", ".state", ".state/sessions/s1/messages.jsonl", ".tmp", ".tmp/s1/tool_1.txt"]


async def test_an_mcp_admin_reads_reserved_paths_through_every_reader():
    """``get_workspace_file_info`` is the third reader; ``read_workspace_file`` is covered above for the run-shaped callers."""
    ts, _ = _toolset()
    with as_mcp("admin"):
        info = await _call(ts, "get_workspace_file_info", {"workspace_id": "w", "path": RESERVED_FILE}, None)
        read = await _call(ts, "read_workspace_file", {"workspace_id": "w", "path": RESERVED_FILE}, None)

    assert not info.is_error, info.output
    assert not read.is_error, read.output


@pytest.mark.parametrize("tool", ["read_workspace_file", "get_workspace_file_info"])
async def test_a_non_admin_mcp_caller_is_refused_a_reserved_path(tool):
    ts, registry = _toolset()
    with as_mcp("user"):
        result = await _call(ts, tool, {"workspace_id": "w", "path": RESERVED_FILE}, None)

    assert result.is_error and _type(result) == "forbidden", result.output
    assert registry.ws.reads == []


@pytest.mark.parametrize("tool", ["read_workspace_file", "get_workspace_file_info"])
async def test_a_tool_context_with_no_initiator_does_not_inherit_the_mcp_admin(tool):
    ts, registry = _toolset()
    with as_mcp("admin"):
        result = await _call(ts, tool, {"workspace_id": "w", "path": RESERVED_FILE}, _no_initiator_ctx())

    assert result.is_error and _type(result) == "forbidden", result.output
    assert registry.ws.reads == []


# ---- the admin side of the listing filter: the runtime's own trees ARE listed ------------------------------------------------------------


async def _list(ts, ctx, **extra):
    result = await _call(ts, "list_workspace_files", {"workspace_id": "w", "path": ".", **extra}, ctx)
    assert not result.is_error, result.output
    return [i["path"] for i in json.loads(result.output)["items"]]


@pytest.mark.parametrize("ctx", [caller("admin"), caller(None, kind="system")], ids=["admin-run", "system"])
async def test_an_admin_run_sees_the_reserved_trees_in_a_recursive_listing(ctx):
    ts, _ = _toolset()

    assert await _list(ts, ctx, recursive=True) == ALL_ENTRIES


async def test_an_mcp_admin_sees_the_reserved_trees_in_a_recursive_and_a_root_listing():
    ts, _ = _toolset()
    with as_mcp("admin"):
        recursive = await _list(ts, None, recursive=True)
        root = await _list(ts, None)

    assert recursive == ALL_ENTRIES
    assert root == ["src", ".state", ".tmp"]


async def test_the_same_listings_drop_the_reserved_trees_for_a_user_run_and_for_an_mcp_user():
    ts, _ = _toolset()
    with as_mcp("user"):
        mcp_root = await _list(ts, None)
        mcp_recursive = await _list(ts, None, recursive=True)

    assert await _list(ts, caller("user")) == ["src"]
    assert mcp_root == ["src"]
    assert mcp_recursive == ["src", "src/main.py"]
