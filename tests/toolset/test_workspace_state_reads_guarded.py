"""A-22, the tool half: ``read_workspace_file``, ``get_workspace_file_info``
and ``list_workspace_files`` (``required_role="user"``) do not serve the
runtime's ``.state`` / ``.tmp`` trees unless the CALLER is an admin.

The caller is the run's ``initiated_by`` under the floor's predicate
(``primer.authz._role_allows``); a call without a ToolContext (the MCP
endpoint dispatches without one) has no known role, so a reserved path
fails closed there. Paths are normalised before the check.
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
