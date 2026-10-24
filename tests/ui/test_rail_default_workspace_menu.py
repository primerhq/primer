"""The console never offers to delete the default workspace (pivot P2 Task 13): the rail's workspace menu, and every other delete site.

The server refuses the delete (``WorkspaceRegistry.destroy`` answers 403 ``reserved_workspace`` for the default workspace), so a Delete the console
offered for it could only fail. ``GET /v1/workspaces`` flags the default row with a response-only ``is_default``; the rail's menu reads it. A row
without the flag (another workspace, or an older server that sends none) keeps its Delete.

The last tests pin every place in ``ui/`` that deletes a workspace, so a new one has to be guarded (or listed here) before it ships: the rail menu
(``SH_api.deleteWorkspace``) and the workspace settings' Destroy tab (``WS_DestroyTab``, ``tests/ui/test_workspace_detail_default.py``). No palette
verb deletes a workspace, the Platform view has no Workspaces page any more (``tests/ui/test_platform_nav.py``), and the phone shell has no workspace
delete at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
RAIL = UI / "components" / "console" / "nv-rail.jsx"

_PRELUDE = r"""
var window = globalThis;
window.innerWidth = 1280;
window.innerHeight = 800;
var LOG = { deleted: [], confirms: [], changed: 0, closed: 0, settings: [], created: [] };
var CONFIRM = true;
window.confirmDialog = function (opts) { LOG.confirms.push(opts); return Promise.resolve(CONFIRM); };
var SH_api = { deleteWorkspace: function (wid) { LOG.deleted.push(wid); return Promise.resolve(null); } };
function NV_identity() { return { d: "", color: "" }; }
function menuProps(w) {
  return {
    workspace: w, x: 0, y: 0,
    onClose: function () { LOG.closed += 1; },
    onChanged: function () { LOG.changed += 1; },
    onOpenSettings: function (wid) { LOG.settings.push(wid); },
    onCreateSessionInWorkspace: function (wid) { LOG.created.push(wid); },
  };
}
"""


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(RAIL)


@pytest.fixture
def ctx(code):
    c = mini_react_context(code, _PRELUDE)
    try:
        yield c
    finally:
        c.close()


def _rows(ctx, wid: str) -> list[str]:
    return json.loads(ctx.eval(
        f"JSON.stringify(MR.findAll({json.dumps('nv-rail-ws-menu-')}).map(function (e) {{ return e.props['data-testid']; }}))"
    ))


def test_the_default_workspace_menu_has_no_delete(ctx) -> None:
    ctx.eval('MR.mount(NV_Rail_WorkspaceContextMenu, menuProps({ id: "prime", name: "prime", is_default: true }))')
    assert _rows(ctx, "prime") == ["nv-rail-ws-menu-new-session:prime", "nv-rail-ws-menu-settings:prime"]
    assert ctx.eval('MR.find("nv-rail-ws-menu-delete:prime") === null')


def test_the_default_workspace_keeps_its_other_rows_working(ctx) -> None:
    ctx.eval('MR.mount(NV_Rail_WorkspaceContextMenu, menuProps({ id: "primer", name: "prime", is_default: true }))')
    ctx.eval('MR.click("nv-rail-ws-menu-settings:primer")')
    ctx.eval('MR.click("nv-rail-ws-menu-new-session:primer")')
    assert json.loads(ctx.eval("JSON.stringify(LOG)"))["settings"] == ["primer"]
    assert json.loads(ctx.eval("JSON.stringify(LOG)"))["created"] == ["primer"]


@pytest.mark.parametrize("row", [
    {"id": "ws-1", "is_default": False},
    {"id": "ws-1"},  # an older server sends no flag: the server is still the guard, so the console keeps the action
])
def test_another_workspace_keeps_delete_and_it_still_deletes(ctx, row) -> None:
    ctx.eval(f"MR.mount(NV_Rail_WorkspaceContextMenu, menuProps({json.dumps(row)}))")
    assert _rows(ctx, "ws-1") == [
        "nv-rail-ws-menu-new-session:ws-1", "nv-rail-ws-menu-settings:ws-1", "nv-rail-ws-menu-delete:ws-1",
    ]
    ctx.eval('MR.click("nv-rail-ws-menu-delete:ws-1")')
    ctx.eval("void 0")  # the confirm resolves, then the delete runs
    log = json.loads(ctx.eval("JSON.stringify(LOG)"))
    assert log["confirms"][0]["title"] == "Delete workspace" and log["confirms"][0]["danger"] is True
    assert log["deleted"] == ["ws-1"]


def test_a_flag_that_is_not_true_is_not_the_default(ctx) -> None:
    """Only ``is_default === true`` hides the row: a truthy string from a stranger's server is not the flag."""
    ctx.eval('MR.mount(NV_Rail_WorkspaceContextMenu, menuProps({ id: "ws-2", is_default: "yes" }))')
    assert ctx.eval('MR.find("nv-rail-ws-menu-delete:ws-2") !== null')


# ---- every delete site in ui/ ---------------------------------------------------------------------------------------------------------------------------

def _component_sources() -> dict[str, str]:
    return {
        str(p.relative_to(UI)): p.read_text(encoding="utf-8")
        for p in sorted((UI / "components").rglob("*.jsx"))
    }


# A DELETE whose path is the workspace itself, not one of its sub-resources: `/workspaces/${encodeURIComponent(wid)}` as a template, or
# "/workspaces/" + encodeURIComponent(wid) closed right after.
_WORKSPACE_DELETE = re.compile(
    r'"DELETE",\s*(?:`/workspaces/\$\{encodeURIComponent\(\w+\)\}`|"/workspaces/"\s*\+\s*encodeURIComponent\(\w+\)\s*\))'
)


def test_the_workspace_delete_requests_are_the_two_known_ones() -> None:
    sites = sorted(
        name for name, src in _component_sources().items() for _ in _WORKSPACE_DELETE.finditer(src)
    )
    assert sites == ["components/shell/sh-api.jsx", "components/workspaces.jsx"], (
        "a new request that deletes a workspace: hide it for the row with is_default, then list it here"
    )
    assert "function WS_DestroyTab(" in _component_sources()["components/workspaces.jsx"]


def test_the_rail_menu_is_the_only_caller_of_the_delete_wrapper() -> None:
    callers = sorted(
        name for name, src in _component_sources().items() for _ in re.finditer(r"\bSH_api\.deleteWorkspace\(", src)
    )
    assert callers == ["components/console/nv-rail.jsx"]


def test_no_palette_verb_deletes_a_workspace() -> None:
    ids = set()
    for p in sorted((UI / "components" / "console").glob("*.jsx")):
        ids |= set(re.findall(r'id:\s*"(workspace\.[\w.]+)"', p.read_text(encoding="utf-8")))
    assert ids == {"workspace.switch", "workspace.create"}, ids


def test_the_phone_shell_has_no_workspace_delete() -> None:
    mobile = (UI / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
    assert "deleteWorkspace" not in mobile and "WS_DestroyTab" not in mobile
    assert not _WORKSPACE_DELETE.search(mobile)
