"""A template made in the template overlay goes back to whoever opened it, and the overlay navigates nowhere (platform revamp Plan 08; the ui-e2e failure
of #886 at 3c13d8ca6).

The old form (``WorkspaceTemplateCreateModal``) navigated to ``/workspaces/templates/<id>`` after a create, to the old detail page. Since Task 11 that
address is the template overlay in edit mode, so a create ended in an edit form of the template just made, over the list it was made from
(tests/ui_e2e/test_workspace_chain_create_journey.py saw a second ``.modal``). #886 made the old form navigate nowhere and hand the new row back through
``onCreated``; Task 15 removed the old form, and these pins hold the template overlay (``WT_TemplateOverlay``) that replaced it to the same rule: a create
closes, hands the new row back through ``onSaved`` and navigates nowhere; an edit closes and navigates nowhere. Its callers keep what it hands back: the
templates page refreshes its list, and the new-workspace dialog selects the template it made
(``tests/ui/test_new_workspace_dialog.py::test_create_a_template_hands_the_new_one_back_selected``).

The overlay runs on its own harness (``tests/ui/test_wt_template_overlay.py``), with the router's ``navigate`` and every console navigation recorded.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import base_code, context, js, settle, transpiled
from tests.ui.test_wt_helpers import CHOICES
from tests.ui.test_wt_template_overlay import SHARED_FILE, STUBS, TEMPLATES_FILE

HAND_BACK_STUBS = r"""
var NAVS = [];
window.primerApi.useRouter = function () { return { navigate: function (p) { NAVS.push(["navigate", p]); }, params: {} }; };
function NV_useConsole() {
  return {
    role: ROLE, toast: function (msg, extra) { TOASTS.push([msg, extra || null]); },
    goView: function (a, b) { NAVS.push(["goView", a, b]); }, openOverlay: function (a, b, c) { NAVS.push(["openOverlay", a, b, c]); },
    closeOverlay: function () { NAVS.push(["closeOverlay"]); }, closeOverlayTo: function (v) { NAVS.push(["closeOverlayTo", v]); },
  };
}
HANDLER = function (method, path, body) {
  if (method === "GET" && ROWS[path]) return Promise.resolve(JSON.parse(JSON.stringify(ROWS[path])));
  if (method === "POST" && path === "/workspace_templates") return Promise.resolve(Object.assign(JSON.parse(JSON.stringify(body)), { id: "tpl-new" }));
  return Promise.resolve(body == null ? { ok: true } : JSON.parse(JSON.stringify(body)));
};
"""

LOCAL_ROW = {"id": "tpl-old", "description": "old", "provider_id": "loc", "backend": {"kind": "local"}}


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval(HAND_BACK_STUBS)
    c.eval("serve('ws:provider-choices', { items: [" + json.dumps(CHOICES["loc"]) + "] });")
    try:
        yield c
    finally:
        c.close()


def test_a_create_closes_hands_the_new_row_back_and_navigates_nowhere(ctx):
    ctx.eval("MR.mount(window.WT_TemplateOverlay, { create: true, onClose: onClose, onSaved: onSaved });")
    ctx.eval("setValue('ws-template-description', 'made in the chain'); press('wt-save');")
    settle(ctx)
    assert js(ctx, "CLOSED") == 1 and js(ctx, "NAVS") == []
    assert [s["id"] for s in js(ctx, "SAVED")] == ["tpl-new"] and js(ctx, "SAVED")[0]["description"] == "made in the chain"
    assert [(c["method"], c["path"]) for c in js(ctx, "CALLS")] == [("POST", "/workspace_templates")]
    assert js(ctx, "TOASTS") == [["Template created: tpl-new", None]]


def test_an_edit_closes_and_navigates_nowhere(ctx):
    ctx.eval(f"ROWS['/workspace_templates/tpl-old'] = {json.dumps(LOCAL_ROW)};")
    ctx.eval("MR.mount(window.WT_TemplateOverlay, { row: " + json.dumps(LOCAL_ROW) + ", onClose: onClose, onSaved: onSaved });")
    ctx.eval("setValue('ws-template-description', 'new words'); press('wt-save');")
    settle(ctx)
    assert js(ctx, "CLOSED") == 1 and js(ctx, "NAVS") == []
    assert [(c["method"], c["path"]) for c in js(ctx, "CALLS")] == [("GET", "/workspace_templates/tpl-old"), ("PUT", "/workspace_templates/tpl-old")]
    assert js(ctx, "SAVED")[0]["description"] == "new words"
