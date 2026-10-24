"""The Workspaces page and its create dialog are gone (platform revamp Plan 08, Task 14; design 14.1, the user's decision C1).

The page (``WorkspacesPage``, with its card grid, filters, pager, phone FAB and ``WS_NewWorkspaceModal``) was a second copy of what the studio rail's workspace
menu offers, with a second create dialog. The rail lists the workspaces, the console's one new-workspace dialog makes them (``NV_CreateWorkspaceOverlay``),
and rename, which only the page's cards offered, moved to the workspace's Settings, Config tab (``tests/ui/test_workspace_rename_in_settings.py``). What stays
in ``workspaces.jsx`` is the workspace's Settings overlay (``WorkspaceDetail``) and its tabs.

The bare address ``overlay=workspaces`` (a link pasted from before) says where workspaces went instead of drawing the list (``NV_WorkspacesMoved``), and
nothing under ``ui/`` names the deleted components.

The old workspace template components followed (Plan 08, Task 15; design 14.2): the list page (``WorkspaceTemplatesPage``), its form with the JSON and table
rows (``WorkspaceTemplateCreateModal``) and its detail page (``WorkspaceTemplateDetail``), with their helpers. Platform › Workspace templates
(``WT_TemplatesPage``), the template overlay (``WT_TemplateOverlay``) and the new-workspace dialog replaced them. The bare ``overlay=workspaces:templates``
says the templates have their own page; ``overlay=workspaces:templates:<id>`` is still the template overlay; the docs embed ``workspace-template-form``
mounts the page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, transpiled

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
WORKSPACES = (COMPONENTS / "workspaces.jsx").read_text(encoding="utf-8")
TEMPLATES = (COMPONENTS / "workspaces" / "templates.jsx").read_text(encoding="utf-8")
OVERLAYS_FILE = COMPONENTS / "console" / "nv-overlays.jsx"
GONE = ("WorkspacesPage", "WS_NewWorkspaceModal", "WS_PhasePill")
# The old template components and the helpers only they used (Task 15).
TEMPLATE_GONE = ("WorkspaceTemplatesPage", "WorkspaceTemplateCreateModal", "WorkspaceTemplateDetail", "ContainerMountEditor", "WT_Toggle",
                 "_wtToastErr", "_wtFieldErrorShown", "_emptyForm", "_fromTemplate")

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


# ---- Task 15: the old template list, form and detail page ----------------------------------------------------------------------------------------------


def test_the_old_template_components_are_gone():
    """Design 14.2: the JSON and table form, the old list and the old detail page are gone. The form's row helpers went with them: ``WS_FieldRow`` and
    ``WS_Section`` stay in ``workspaces/shared.jsx`` for the workspace provider form, but ``templates.jsx`` no longer draws them."""
    for gone in TEMPLATE_GONE + ("WS_FieldRow", "WS_Section"):
        assert not re.search(rf"\b{gone}\b", TEMPLATES), gone
    for kept in ("window.WT_TemplatesPage = WT_TemplatesPage;", "window.WT_TemplateOverlay = WT_TemplateOverlay;", "const WT_ADMIN_ONLY_NOTE ="):
        assert kept in TEMPLATES, kept


def test_nothing_under_ui_names_the_deleted_template_components():
    offenders = []
    for path in sorted(UI.rglob("*")):
        if not path.is_file() or "vendor" in path.parts or path.suffix not in {".jsx", ".js", ".html", ".json", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path.relative_to(ROOT)}: {name}" for name in TEMPLATE_GONE if re.search(rf"\b{name}\b", text)]
    assert offenders == []


def test_the_bare_template_address_says_where_templates_went(ctx):
    assert js(ctx, "rendered({ name: 'workspaces', section: 'templates', id: null })") == {"type": "NV_WorkspacesMoved", "props": {"which": "templates"}}


def test_a_template_address_is_still_the_template_overlay(ctx):
    ctx.eval("window.WT_TemplateOverlay = function WT_TemplateOverlay() { return null; };")
    out = js(ctx, "rendered({ name: 'workspaces', section: 'templates', id: 'tpl-1' })")
    assert out == {"type": "WT_TemplateOverlay", "props": {"templateId": "tpl-1"}}


def test_the_bare_template_address_keeps_its_title(ctx):
    assert js(ctx, "NV_overlayTitle({ name: 'workspaces', section: 'templates', id: null })") == "Workspace templates"


def test_the_template_form_embed_mounts_the_templates_page():
    """The docs embed ``workspace-template-form`` mounted the old list; it mounts Platform › Workspace templates now, which takes no props."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        ctx.eval("var window = globalThis;")
        ctx.eval((COMPONENTS / "docs" / "embed-registry.jsx").read_text(encoding="utf-8"))
        entry = json.loads(ctx.eval("JSON.stringify(window.DocsEmbedRegistry['workspace-template-form'])"))
    finally:
        ctx.close()
    assert entry == {"component": "WT_TemplatesPage", "fixtures": "workspace-template-form", "props": {}}
