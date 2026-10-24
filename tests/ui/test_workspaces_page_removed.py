"""The Workspaces page and its create dialog are gone (platform revamp Plan 08, Task 14; design 14.1, the user's decision C1).

The page (``WorkspacesPage``, with its card grid, filters, pager, phone FAB and ``WS_NewWorkspaceModal``) was a second copy of what the studio rail's workspace
menu offers, with a second create dialog. The rail lists the workspaces, the console's one new-workspace dialog makes them (``NV_CreateWorkspaceOverlay``),
and rename, which only the page's cards offered, moved to the workspace's Settings, Config tab (``tests/ui/test_workspace_rename_in_settings.py``). What stays
in ``workspaces.jsx`` is the workspace's Settings overlay (``WorkspaceDetail``) and its tabs.

The bare address ``overlay=workspaces`` (a link pasted from before) says where workspaces went instead of drawing the list (``NV_WorkspacesMoved``), and
nothing under ``ui/`` names the deleted components.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, transpiled

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
WORKSPACES = (COMPONENTS / "workspaces.jsx").read_text(encoding="utf-8")
OVERLAYS_FILE = COMPONENTS / "console" / "nv-overlays.jsx"
GONE = ("WorkspacesPage", "WS_NewWorkspaceModal", "WS_PhasePill")

STUBS = r"""
var CON = { closed: 0, to: [] };
function NV_useConsole() {
  return { role: "admin", closeOverlay: function () { CON.closed += 1; }, closeOverlayTo: function (v) { CON.to.push(v); } };
}
window.WorkspaceDetail = function WorkspaceDetail() { return null; };
var SHELL = { closeOverlay: function () {}, openOverlay: function () {}, openDoc: function () {}, switchWorkspace: function () {} };
function rendered(state) { var el = NV_OVERLAY_MOUNTS.workspaces.render(state, SHELL); return { type: el.type === NV_WorkspacesMoved ? "NV_WorkspacesMoved" : el.type === window.WorkspaceDetail ? "WorkspaceDetail" : String(el.type && el.type.name), props: el.props }; }
"""


@pytest.fixture
def ctx():
    c = context(base_code() + "\n" + transpiled(OVERLAYS_FILE))
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


def test_the_page_and_its_create_dialog_are_gone_from_workspaces_jsx():
    for gone in ("function WorkspacesPage(", "function WS_NewWorkspaceModal(", "function WS_PhasePill(", "window.WorkspacesPage", "workspace-template-picker"):
        assert gone not in WORKSPACES, gone
    assert "window.WorkspaceDetail = WorkspaceDetail;" in WORKSPACES
    assert "function WS_RenameWorkspaceModal(" in WORKSPACES, "rename stays: the Settings overlay opens it"


def test_nothing_under_ui_names_the_deleted_components():
    offenders = []
    for path in sorted(UI.rglob("*")):
        if not path.is_file() or "vendor" in path.parts or path.suffix not in {".jsx", ".js", ".html", ".json", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path.relative_to(ROOT)}: {name}" for name in GONE if re.search(rf"\b{name}\b", text)]
    assert offenders == []


def test_the_docs_embed_of_the_page_is_gone():
    registry = (COMPONENTS / "docs" / "embed-registry.jsx").read_text(encoding="utf-8")
    assert '"workspaces": {' not in registry


def test_the_bare_address_says_where_workspaces_went(ctx):
    assert js(ctx, "rendered({ name: 'workspaces', section: null, id: null })") == {"type": "NV_WorkspacesMoved", "props": {"which": "workspaces"}}


def test_a_workspace_address_still_opens_its_settings(ctx):
    out = js(ctx, "rendered({ name: 'workspaces', section: 'config', id: 'ws-1' })")
    assert out["type"] == "WorkspaceDetail" and out["props"]["workspaceId"] == "ws-1"


def test_the_moved_notice_for_workspaces(ctx):
    ctx.eval("MR.mount(NV_WorkspacesMoved, { which: 'workspaces' });")
    note = js(ctx, "MR.find('nv-ws-moved:workspaces').props.className")
    assert "nv-bind-empty" in note
    assert ctx.eval("textOf(MR.find('nv-ws-moved:workspaces'))").startswith(
        "Workspaces are listed in the studio: pick one from the workspace menu in the rail.")
    assert ctx.eval("textOf(nativeButton('nv-ws-moved-close'))") == "Close"
    ctx.eval("press('nv-ws-moved-close');")
    assert js(ctx, "CON") == {"closed": 1, "to": []}


def test_the_moved_notice_for_templates(ctx):
    """The same notice, for the bare template list address (Task 15 mounts it there): one press closes the overlay onto the Workspace templates page."""
    ctx.eval("MR.mount(NV_WorkspacesMoved, { which: 'templates' });")
    assert ctx.eval("textOf(MR.find('nv-ws-moved:templates'))").startswith("Workspace templates have their own page now.")
    assert ctx.eval("textOf(nativeButton('nv-ws-moved-open'))") == "Open Workspace templates"
    ctx.eval("press('nv-ws-moved-open');")
    assert js(ctx, "CON") == {"closed": 0, "to": [{"name": "platform", "nav": "templates"}]}


def test_the_bare_address_keeps_its_title(ctx):
    assert js(ctx, "NV_overlayTitle({ name: 'workspaces', section: null, id: null })") == "Workspaces"
