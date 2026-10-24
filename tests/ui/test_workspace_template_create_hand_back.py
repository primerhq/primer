"""A template made from the old create form goes back to whoever opened the form (platform revamp Plan 08; the ui-e2e failure of #886 at 3c13d8ca6).

The old form (``WorkspaceTemplateCreateModal``, until Plan 08 Task 15 removes it) navigated to ``/workspaces/templates/<id>`` after a create, to the old
detail page. Since Task 11 that address is the template overlay in edit mode, so a create ended in an edit form of the template just made, over the list it
was made from (tests/ui_e2e/test_workspace_chain_create_journey.py saw a second ``.modal``). The template overlay's own rule (Task 10) is the one that
holds: a create closes and returns to its caller. So the old form navigates nowhere, and a caller that wants the new row takes it through ``onCreated``:
the old new-workspace dialog's "Create a template now" chain gets its new template back, selected.

The real ``workspaces/shared.jsx``, ``templates.jsx`` and ``workspaces.jsx`` run here on the overlay tests' harness (``tests/ui/_register_harness.py``),
with the router's ``navigate`` recorded.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled
from tests.ui.test_wt_template_overlay import SHARED_FILE, STUBS, TEMPLATES_FILE

WORKSPACES_FILE = COMPONENTS / "workspaces.jsx"

HAND_BACK_STUBS = r"""
var NAVS = [], CREATED = [];
window.primerApi.useRouter = function () { return { navigate: function (p) { NAVS.push(p); }, params: {} }; };
window.primerApi.useViewport = function () { return { isMobile: false }; };
HANDLER = function (method, path, body) {
  if (method === "POST" && path === "/workspace_templates") return Promise.resolve(Object.assign({ id: "tpl-new" }, JSON.parse(JSON.stringify(body))));
  return Promise.resolve(body == null ? { ok: true } : JSON.parse(JSON.stringify(body)));
};
function buttonWithText(root, text) {
  return ALL(root).filter(function (n) { return n.type === "button" && textOf(n).trim() === text; })[0] || null;
}
function click(node) { node.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} }); MR.rerender(); }
"""

LOCAL_PROVIDER = {"id": "loc", "provider": "local"}


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE, WORKSPACES_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval(HAND_BACK_STUBS)
    c.eval("serve('ws:templates:providers-picker', { items: [" + json.dumps(LOCAL_PROVIDER) + "] });")
    try:
        yield c
    finally:
        c.close()


def _create_in_the_old_form(ctx) -> None:
    ctx.eval("setValue('ws-template-description', 'made in the chain');")
    ctx.eval("click(buttonWithText('ws-template-modal', 'Create'));")
    settle(ctx)


def test_the_new_workspace_chain_gets_its_new_template_back_selected(ctx):
    """New workspace, no template yet, "Create a template now": the template is made, the form closes, nothing navigates, and the dialog is back with
    the NEW template selected (not the first row of the refreshed list, which may be someone else's)."""
    ctx.eval("serve('workspaces:templates', { items: [] });")
    ctx.eval("MR.mount(WS_NewWorkspaceModal, { onClose: function () { CLOSED += 1; }, pushToast: function () {}, onCreated: function (id) { CREATED.push(id); } });")
    ctx.eval("click(MR.findProp('children', 'Create a template now'));")
    assert ctx.eval("!!MR.find('ws-template-modal')")
    _create_in_the_old_form(ctx)
    assert [c["method"] + " " + c["path"] for c in js(ctx, "CALLS")] == ["POST /workspace_templates"]
    assert js(ctx, "NAVS") == [], "no navigation: the template's address is the template overlay in edit mode now"
    assert ctx.eval("MR.find('ws-template-modal') === null"), "the form closed"
    assert js(ctx, "RES['workspaces:templates'].refetched") >= 1, "the dialog asks for the templates again"
    ctx.eval("serve('workspaces:templates', { items: [{ id: 'a-made-meanwhile', backend: { kind: 'local' } }, { id: 'tpl-new', backend: { kind: 'local' } }] });"
             " MR.rerender();")
    assert js(ctx, "MR.find('workspace-template-row-tpl-new').props['data-selected']") == "true"
    assert js(ctx, "MR.find('workspace-template-row-a-made-meanwhile').props['data-selected']") == "false"
    assert js(ctx, "CLOSED") == 0, "the new-workspace dialog itself stays open"


def test_a_create_from_the_old_list_closes_and_navigates_nowhere(ctx):
    ctx.eval("MR.mount(window.WorkspaceTemplateCreateModal, { onClose: function () { CLOSED += 1; }, pushToast: function () {} });")
    _create_in_the_old_form(ctx)
    assert js(ctx, "CLOSED") == 1 and js(ctx, "NAVS") == []
    assert [c["path"] for c in js(ctx, "CALLS")] == ["/workspace_templates"]


def test_an_edit_in_the_old_form_still_navigates_nowhere_new(ctx):
    """The old form's edit closed and stayed where it was before this change too; only the create navigated."""
    row = {"id": "tpl-old", "description": "old", "provider_id": "loc", "backend": {"kind": "local"}}
    ctx.eval("MR.mount(window.WorkspaceTemplateCreateModal, { existing: " + json.dumps(row) + ", onClose: function () { CLOSED += 1; }, pushToast: function () {} });")
    ctx.eval("setValue('ws-template-description', 'new words'); click(buttonWithText('ws-template-modal', 'Save'));")
    settle(ctx)
    assert js(ctx, "CLOSED") == 1 and js(ctx, "NAVS") == []
