"""The ``list_workspaces`` and ``get_workspace`` tools carry ``is_default`` on every row (lead ruling 5 of the G7 fixes, audit G7-09).

``docs/agents/workspaces.md`` told agents to find the default workspace (prime) "by that name". But prime can be renamed, any workspace may be
called ``prime``, and no tool returned the default id: only the REST rows carried the response-only ``is_default``. The tool rows now carry the same
flag, read from the system state as REST reads it, so an agent finds prime by ``is_default: true``. Calls go through ``dispatch_inner_tool``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.conftest import _FakeStorageProvider


def _row(wid: str, name: str) -> Workspace:
    return Workspace(
        id=wid, name=name, template_id="local-default", provider_id="local", created_at=datetime(2026, 1, 1, tzinfo=UTC), phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token"),
    )


async def _call(sp, tool: str, arguments: dict):
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=object())  # type: ignore[arg-type]
    result = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=tool, arguments=arguments, ctx=None)
    assert not result.is_error, result.output
    return json.loads(result.output)


async def _sp():
    """The default workspace (``primer``, renamed ``ops``) and an ordinary workspace somebody called ``prime``."""
    sp = _FakeStorageProvider()
    await sp.get_storage(Workspace).create(_row("primer", "ops"))
    await sp.get_storage(Workspace).create(_row("ws-2", "prime"))
    await sp.set_default_workspace_id("primer")
    return sp


async def test_list_workspaces_marks_the_default_workspace_and_only_it():
    body = await _call(await _sp(), "list_workspaces", {})
    assert {row["id"]: (row["name"], row["is_default"]) for row in body["items"]} == {"primer": ("ops", True), "ws-2": ("prime", False)}
    assert body["total"] == 2


async def test_get_workspace_carries_the_flag_too():
    sp = await _sp()
    assert (await _call(sp, "get_workspace", {"id": "primer"}))["is_default"] is True
    other = await _call(sp, "get_workspace", {"id": "ws-2"})
    assert other["is_default"] is False and other["name"] == "prime"


async def test_with_no_default_recorded_and_no_legacy_row_no_row_is_the_default():
    sp = _FakeStorageProvider()
    await sp.get_storage(Workspace).create(_row("ws-2", "prime"))
    body = await _call(sp, "list_workspaces", {})
    assert [row["is_default"] for row in body["items"]] == [False]
