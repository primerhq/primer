"""The new-workspace dialog, rebuilt with the platform kit (platform revamp Plan 08, Task 13; design 14.3): ``NV_CreateWorkspaceOverlay``.

One kit form overlay, "New workspace". "From template" at the centre top is the kit selector, searchable, over EVERY template (a page of 200 at a time,
``WT_fetchAllTemplates``); under it the template's summary line, Name and Id, a SIZE section for a provider that takes one (pre-filled from the template),
EXTRA ENVIRONMENT, and folds for extra files and extra start-up commands. What it must never do:

* offer only the first 200 templates (Review Focus 3);
* send a size the person did not change, or one the provider cannot take (Review Focus 4; the body is ``NV_nwBody``);
* hide a refusal: one the dialog has a field for lands under it, any other in the one line above the footer, in words, never a code (Review Focus 2);
* turn Create natively disabled for the length of its request, or send twice;
* leave the dialog for another page to make a template: "Create a template" opens the template overlay over the dialog and hands the new template back,
  selected (lead ruling 2 of the slice; #886 round 3 added the hand-back for the old dialog).

The context holds the REAL ``api.js``, ``shared.jsx``, ``shared/form-field.jsx``, the platform kit, ``workspaces/shared.jsx``, ``templates.jsx`` and
``nv-overlays.jsx`` (``tests/ui/_register_harness.py``), with the network, the console and ``useMutation`` stubbed. Every refusal is the real router's
(``tests/ui/_wt_envelopes.py``).
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled
from tests.ui._wt_envelopes import envelope
from tests.ui.test_wt_helpers import CHOICES
from tests.ui.test_wt_template_overlay import SHARED_FILE, TEMPLATES_FILE

OVERLAYS_FILE = COMPONENTS / "console" / "nv-overlays.jsx"
KEY = "nv-ov:workspace-templates"
GIB = 2**30

K8S = {
    "id": "tpl-k3s", "description": "Python 3.12 with uv", "provider_id": "k3s",
    "backend": {"kind": "kubernetes", "image": "rt:1.4", "pvc_size": "10Gi"},
    "resources": {"cpu_cores": 2, "memory_bytes": 4 * GIB},
}
DOCKER = {"id": "tpl-dock", "description": "Docker", "provider_id": "dock", "backend": {"kind": "container", "cpu_cores": 1, "memory_bytes": 2 * GIB}}
LOCAL = {"id": "tpl-loc", "description": "Local", "provider_id": "loc", "backend": {"kind": "local"}}

STUBS = r"""
var ROLE = "admin";
var OVERLAY_CLOSED = 0, VERB_RUNS = [], INVALIDATES = [];
function NV_useConsole() {
  return {
    role: ROLE, wid: "ws-1",
    toast: function (msg, extra) { TOASTS.push([msg, extra || null]); },
    closeOverlay: function () { OVERLAY_CLOSED += 1; },
    registry: { get: function (id) { return { id: id, run: function (arg) { VERB_RUNS.push([id, arg === undefined ? null : arg]); } }; } },
  };
}
window.primerApi.useMutation = function (fn, opts) {
  var state = React.useState(false);
  var inv = JSON.stringify((opts && opts.invalidates) || []);
  if (INVALIDATES.indexOf(inv) < 0) INVALIDATES.push(inv);
  return {
    mutate: function (arg) {
      state[1](true);
      return fn(arg).then(function (answer) {
        state[1](false);
        if (opts && opts.onSuccess) opts.onSuccess(answer);
        return answer;
      }, function (err) {
        state[1](false);
        if (opts && opts.onError) opts.onError(err);
        throw err;
      });
    },
    loading: state[0]
  };
};
// The null key disables a resource, as the real useResource does.
var __useResource = window.primerApi.useResource;
window.primerApi.useResource = function (key, fetcher, opts) {
  if (key == null) return { data: undefined, error: null, loading: false, refetch: function () {} };
  return __useResource(key, fetcher, opts);
};
var FORMS = {};
var __PKFO = window.PK_FormOverlay;
window.PK_FormOverlay = function (p) { FORMS[p.testid] = p; return __PKFO(p); };
function dialog() { return FORMS["nv-overlay:new-workspace"]; }
// The error under a field of the dialog: the alert in the kit field that holds the control, or a table's own line.
function errorUnder(testid) {
  var row = fieldRow("nv-overlay:new-workspace", testid);
  if (row) { var hit = row.filter(function (c) { return c.role === "alert"; })[0]; return hit ? hit.text : null; }
  var line = MR.find(testid + "-error");
  return line ? textOf(line) : null;
}
function helpUnder(testid) {
  var row = fieldRow("nv-overlay:new-workspace", testid) || [];
  var hit = row.filter(function (c) { return String(c.className).indexOf("field-help") >= 0 && c.role !== "alert"; })[0];
  return hit ? hit.text : null;
}
function value(testid) { var n = MR.find(testid); return n ? n.props.value : null; }
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE, OVERLAYS_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval("serve('ws:provider-choices', { items: " + json.dumps([CHOICES["loc"]]) + " });")
    try:
        yield c
    finally:
        c.close()


def _mount(ctx, templates: list | None = None, role: str = "admin") -> None:
    if templates is not None:
        ctx.eval(f"serve({json.dumps(KEY)}, {{ items: {json.dumps(templates)} }});")
    ctx.eval(f"ROLE = {json.dumps(role)}; MR.mount(window.NV_CreateWorkspaceOverlay, {{}});")


def _pick(ctx, template_id: str) -> None:
    """Open the "From template" selector and click the template's item (the kit's own test id, ``<testid>:<value>``)."""
    ctx.eval(f"MR.click('nv-nw-template'); MR.click({json.dumps('nv-nw-template:' + template_id)});")


def _posts(ctx, path: str = "/workspaces") -> list:
    return [c for c in js(ctx, "CALLS") if c["method"] == "POST" and c["path"] == path]


def _answer_create_with(ctx, row: dict) -> None:
    ctx.eval("HANDLER = function (m, p, b) { if (m === 'POST' && p === '/workspaces') return Promise.resolve(" + json.dumps(row) + ");"
             " return Promise.resolve(b == null ? { ok: true } : JSON.parse(JSON.stringify(b))); };")


def _refuse_create_with(ctx, env: dict) -> None:
    ctx.eval("var ENV = " + json.dumps(env) + "; HANDLER = function (m, p, b) { if (m === 'POST' && p === '/workspaces') return refuse(ENV);"
             " return Promise.resolve(b == null ? { ok: true } : b); };")


# ---- the dialog and its template dropdown -------------------------------------------------------------------------------------------------------------


def test_the_dialog_is_the_kit_form_overlay_with_a_searchable_template_dropdown(ctx):
    _mount(ctx, [K8S, DOCKER, LOCAL])
    d = js(ctx, "{ title: dialog().title, testid: dialog().testid, selector: dialog().selector }")
    assert d["title"] == "New workspace" and d["testid"] == "nv-overlay:new-workspace"
    sel = d["selector"]
    assert (sel["prefix"], sel["searchable"], sel["testid"], sel["value"]) == ("From template", True, "nv-nw-template", "tpl-k3s")
    assert [(o["value"], o["label"]) for o in sel["options"]] == [("tpl-k3s", "tpl-k3s"), ("tpl-dock", "tpl-dock"), ("tpl-loc", "tpl-loc")]
    assert [o["iconPath"] for o in sel["options"]] == js(ctx, "[WT_KIND_ICONS.kubernetes, WT_KIND_ICONS.container, WT_KIND_ICONS.local]")
    assert ctx.eval("!!MR.find('nv-overlay:new-workspace')")


def test_every_template_is_offered_past_200(ctx):
    """Review Focus 3: the dialog's own fetcher pages through every template; 450 give 450 options, and the selector's search finds the last one."""
    _mount(ctx)
    assert ctx.eval("!!MR.find('nv-nw-loading')") and js(ctx, "dialog().selector") is None, "no selector until the list has arrived"
    ctx.eval(
        "HANDLER = function (m, p) { var offset = Number(/offset=(\\d+)/.exec(p)[1]); var items = [];"
        " for (var i = offset; i < Math.min(450, offset + 200); i++) items.push({ id: 't-' + String(i).padStart(3, '0'), provider_id: 'loc', backend: { kind: 'local' } });"
        " return Promise.resolve({ items: items }); };"
        f"RES[{json.dumps(KEY)}].fetcher(null).then(function (r) {{ serve({json.dumps(KEY)}, r); }});"
    )
    settle(ctx)
    gets = [c["path"] for c in js(ctx, "CALLS") if c["method"] == "GET"]
    assert len(gets) == 3 and all("limit=200" in p and "order_by=id:asc" in p for p in gets)
    sel = js(ctx, "dialog().selector")
    assert len(sel["options"]) == 450 and sel["searchable"] is True and sel["testid"] == "nv-nw-template"
    ctx.eval("MR.click('nv-nw-template'); setValue('nv-nw-template:search', 't-449');")
    assert js(ctx, "MR.findAll('nv-nw-template:t-').map(function (n) { return n.props['data-testid']; })") == ["nv-nw-template:t-449"]
    ctx.eval("MR.click('nv-nw-template:t-449');")
    assert js(ctx, "dialog().selector.value") == "t-449"


def test_the_first_template_is_picked_once_the_list_arrives(ctx):
    _mount(ctx)
    assert ctx.eval("textOf(MR.find('nv-nw-loading'))") == "Loading templates…"
    ctx.eval(f"serve({json.dumps(KEY)}, {{ items: {json.dumps([DOCKER, K8S])} }}); MR.rerender();")
    assert js(ctx, "dialog().selector.value") == "tpl-dock"


def test_the_summary_follows_the_template(ctx):
    _mount(ctx, [K8S, DOCKER, LOCAL])
    assert ctx.eval("textOf(MR.find('nv-nw-summary'))") == "Python 3.12 with uv · Kubernetes on k3s · 2 CPU · 4 GiB · 10 GiB disk"
    _pick(ctx, "tpl-loc")
    assert ctx.eval("textOf(MR.find('nv-nw-summary'))") == "Local · Local on loc"


def test_size_section_by_kind(ctx):
    _mount(ctx, [K8S, DOCKER, LOCAL])
    assert [js(ctx, f"value('{t}')") for t in ("nv-nw-cpu", "nv-nw-memory", "nv-nw-disk")] == ["2", "4", "10"]
    assert js(ctx, "MR.find('nv-nw-cpu').props.placeholder") == "No limit"
    assert ctx.eval("textOf(MR.find('nv-nw-size-help'))") == "Filled in from the template. A change applies to this workspace only."
    _pick(ctx, "tpl-dock")
    assert [js(ctx, f"value('{t}')") for t in ("nv-nw-cpu", "nv-nw-memory")] == ["1", "2"]
    assert ctx.eval("MR.find('nv-nw-disk') === null"), "Docker cannot limit a disk, so the dialog does not offer one"
    _pick(ctx, "tpl-loc")
    assert ctx.eval("MR.find('nv-nw-size') === null")


def test_a_new_template_resets_the_sizes_the_person_typed(ctx):
    _mount(ctx, [K8S, DOCKER])
    ctx.eval("setValue('nv-nw-memory', '8');")
    _pick(ctx, "tpl-dock")
    _pick(ctx, "tpl-k3s")
    assert js(ctx, "value('nv-nw-memory')") == "4"
    _answer_create_with(ctx, {"id": "ws-1"})
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert _posts(ctx)[0]["body"] == {"template_id": "tpl-k3s"}


# ---- what Create sends ---------------------------------------------------------------------------------------------------------------------------------


def test_untouched_sizes_are_not_sent(ctx):
    """Review Focus 4 at the dialog: a pre-filled 500Mi disk (0.49 GiB) and 1.5 GiB of memory that nobody touched are not sent."""
    odd = {**K8S, "backend": {**K8S["backend"], "pvc_size": "500Mi"}, "resources": {"cpu_cores": 2, "memory_bytes": 1610612736}}
    _mount(ctx, [odd])
    assert [js(ctx, f"value('{t}')") for t in ("nv-nw-cpu", "nv-nw-memory", "nv-nw-disk")] == ["2", "1.5", "0.49"]
    _answer_create_with(ctx, {"id": "ws-1"})
    ctx.eval("setValue('nv-nw-cpu', '3'); setValue('nv-nw-cpu', '2'); press('nv-nw-create');")
    settle(ctx)
    assert _posts(ctx)[0]["body"] == {"template_id": "tpl-k3s"}, "typing a size back to what was filled in is no change"


def test_create_sends_only_what_the_user_changed(ctx):
    _mount(ctx, [K8S, DOCKER, LOCAL])
    _answer_create_with(ctx, {"id": "ws-9"})
    ctx.eval("setValue('nv-nw-memory', '8'); MR.click('nv-nw-env:add'); setValue('nv-nw-env:0:key', 'TOKEN'); setValue('nv-nw-env:0:value', 'x');")
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert _posts(ctx)[0]["body"] == {"template_id": "tpl-k3s", "overrides": {"env": {"TOKEN": "x"}, "resources": {"memory_bytes": 8 * GIB}}}


def test_name_id_extra_files_and_commands_are_sent(ctx):
    _mount(ctx, [LOCAL])
    assert ctx.eval("MR.find('nv-nw-files') === null && MR.find('nv-nw-init') === null"), "both folds start closed"
    ctx.eval("setValue('nv-nw-name', 'payments refactor'); setValue('nv-nw-id', 'pay-1');")
    ctx.eval("MR.click('nv-nw-files-fold:toggle'); MR.click('nv-nw-files-add'); setValue('nv-nw-files-path:0', 'hello.txt'); setValue('nv-nw-files-content:0', 'hi');")
    ctx.eval("MR.click('nv-nw-init-fold:toggle'); setValue('nv-nw-init', 'echo hi > init.txt\\n');")
    assert ctx.eval("textOf(MR.find('nv-nw-files-fold:toggle'))").strip() == "▾ Extra files"
    assert js(ctx, "helpUnder('nv-nw-init')") == "One per line; they run after the template's own"
    _answer_create_with(ctx, {"id": "pay-1", "name": "payments refactor"})
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert _posts(ctx)[0]["body"] == {
        "template_id": "tpl-loc", "name": "payments refactor", "id": "pay-1",
        "overrides": {"files": [{"path": "hello.txt", "source": {"kind": "inline", "content": "hi"}, "mode": None}], "init_commands": ["echo hi > init.txt"]},
    }


def test_the_env_values_are_hidden_and_the_table_says_so(ctx):
    _mount(ctx, [LOCAL])
    table = js(ctx, "MR.findProp('testid', 'nv-nw-env').props")
    assert table["title"] == "EXTRA ENVIRONMENT" and table["addLabel"] == "+ Add"
    assert table["columns"] == [{"key": "key", "label": "Name", "input": "text"}, {"key": "value", "label": "Value", "input": "secret"}]
    assert ctx.eval("textOf(MR.find('nv-overlay:new-workspace'))").count("Added to the template's; values are hidden") == 1


def test_id_is_checked_as_you_type(ctx):
    _mount(ctx, [LOCAL])
    assert js(ctx, "helpUnder('nv-nw-id')") == "Made for you if left empty. Lowercase letters, digits and dashes."
    assert js(ctx, "helpUnder('nv-nw-name')") == "Shown in the workspace menu in place of the id"
    ctx.eval("setValue('nv-nw-id', 'Bad_Id');")
    assert js(ctx, "errorUnder('nv-nw-id')") == "Use lowercase letters, digits and dashes, starting and ending with a letter or digit (at most 63)."
    assert js(ctx, "nativeButton('nv-nw-create').props.disabled") is True
    ctx.eval("press('nv-nw-create');")
    assert _posts(ctx) == []
    ctx.eval("setValue('nv-nw-id', 'good-id');")
    assert js(ctx, "errorUnder('nv-nw-id')") is None and not js(ctx, "nativeButton('nv-nw-create').props.disabled")
    _answer_create_with(ctx, {"id": "good-id"})
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert _posts(ctx)[0]["body"] == {"template_id": "tpl-loc", "id": "good-id"}


def test_a_size_and_an_env_name_are_checked_as_you_type(ctx):
    _mount(ctx, [K8S])
    ctx.eval("setValue('nv-nw-cpu', '0');")
    assert js(ctx, "errorUnder('nv-nw-cpu')") == "Enter a positive number"
    ctx.eval("setValue('nv-nw-cpu', '');")
    assert js(ctx, "errorUnder('nv-nw-cpu')") is None, "a cleared size is the template's again, not an error"
    ctx.eval("MR.click('nv-nw-env:add'); MR.click('nv-nw-env:add'); setValue('nv-nw-env:0:key', 'A'); setValue('nv-nw-env:1:key', 'A');")
    assert js(ctx, "errorUnder('nv-nw-env')") == "Each name once" and js(ctx, "nativeButton('nv-nw-create').props.disabled") is True


# ---- refusals ------------------------------------------------------------------------------------------------------------------------------------------


def test_a_server_field_error_lands_under_its_field(ctx):
    """Review Focus 2: the real 422 for a Kubernetes disk_bytes; its msg shows under Disk and the line above the footer stays empty."""
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "tpl-k3s", "overrides": {"resources": {"disk_bytes": 0}}})
    assert status == 422
    _mount(ctx, [K8S])
    _refuse_create_with(ctx, env)
    ctx.eval("setValue('nv-nw-disk', '20'); press('nv-nw-create');")
    settle(ctx)
    assert js(ctx, "errorUnder('nv-nw-disk')") == env["extensions"]["errors"][0]["msg"]
    assert ctx.eval("MR.find('nv-nw-error') === null")
    assert js(ctx, "OVERLAY_CLOSED") == 0 and js(ctx, "VERB_RUNS") == []
    ctx.eval("setValue('nv-nw-disk', '30');")
    assert js(ctx, "errorUnder('nv-nw-disk')") is None, "editing the field clears the server's refusal of it"


def test_a_refusal_for_a_hidden_field_is_one_line_above_the_footer(ctx):
    """Review Focus 2: the dialog never shows Disk for Docker, so the real 422 for disk_bytes is the one line above the footer."""
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "tpl-dock", "overrides": {"resources": {"disk_bytes": 10 * GIB}}})
    assert status == 422
    _mount(ctx, [DOCKER])
    _refuse_create_with(ctx, env)
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert ctx.eval("textOf(MR.find('nv-nw-error'))") == "Docker cannot limit a workspace's disk. Remove this override."
    assert js(ctx, "MR.find('nv-nw-error').props.role") == "alert"
    assert js(ctx, "errorUnder('nv-nw-cpu')") is None and js(ctx, "errorUnder('nv-nw-memory')") is None


def test_a_refused_env_name_lands_under_the_environment_table(ctx):
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "tpl-k3s", "overrides": {"env": {"PRIMER_RUNTIME_TOKEN": "x"}}})
    assert status == 422
    _mount(ctx, [K8S])
    _refuse_create_with(ctx, env)
    ctx.eval("MR.click('nv-nw-env:add'); setValue('nv-nw-env:0:key', 'PRIMER_RUNTIME_TOKEN'); setValue('nv-nw-env:0:value', 'x'); press('nv-nw-create');")
    settle(ctx)
    assert js(ctx, "errorUnder('nv-nw-env')") == env["extensions"]["errors"][0]["msg"]
    assert ctx.eval("MR.find('nv-nw-error') === null")


def test_a_refusal_with_no_field_is_said_in_words(ctx):
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "no-such-template"})
    assert status == 404
    _mount(ctx, [LOCAL])
    _refuse_create_with(ctx, env)
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    line = ctx.eval("textOf(MR.find('nv-nw-error'))")
    assert line == js(ctx, "window.primerApi.readRefusal(new window.primerApi.ApiError(ENV)).message") == "WorkspaceTemplate 'no-such-template' does not exist"


def test_a_refusal_of_an_extra_file_opens_its_fold(ctx):
    """A refusal of a field inside a closed fold opens the fold, so the error is never drawn where nobody can see it."""
    env = {
        "type": "/errors/validation-error", "title": "Validation Error", "status": 422, "detail": "overrides.files.0.path: Field required",
        "extensions": {"errors": [{"loc": ["body", "overrides", "files", 0, "path"], "msg": "Field required", "type": "missing"}]},
    }
    _mount(ctx, [LOCAL])
    _refuse_create_with(ctx, env)
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert ctx.eval("!!MR.find('nv-nw-files')") and js(ctx, "errorUnder('nv-nw-files')") == "Field required"


# ---- after Create --------------------------------------------------------------------------------------------------------------------------------------


def test_success_toasts_and_switches(ctx):
    _mount(ctx, [LOCAL])
    _answer_create_with(ctx, {"id": "ws-9", "name": "spike"})
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert js(ctx, "TOASTS") == [["Workspace created: spike", None]]
    assert js(ctx, "VERB_RUNS") == [["workspace.switch", {"wid": "ws-9"}]]
    assert js(ctx, "OVERLAY_CLOSED") == 1
    assert '["nv-workspaces"]' in js(ctx, "INVALIDATES"), "the rail's workspace list is asked again"


def test_a_workspace_with_no_name_is_toasted_by_its_id(ctx):
    _mount(ctx, [LOCAL])
    _answer_create_with(ctx, {"id": "ws-10", "name": None})
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert js(ctx, "TOASTS") == [["Workspace created: ws-10", None]]


def test_create_is_busy_not_disabled_while_the_request_is_out(ctx):
    _mount(ctx, [LOCAL])
    ctx.eval("HANDLER = function () { return new Promise(function () {}); }; press('nv-nw-create');")
    settle(ctx)
    create = js(ctx, "nativeButton('nv-nw-create').props")
    assert create.get("aria-busy") == "true" and not create.get("disabled")
    assert ctx.eval("textOf(nativeButton('nv-nw-create'))") == "Creating…"
    cancel = js(ctx, "nativeButton('nv-nw-cancel').props")
    assert cancel.get("aria-disabled") == "true" and not cancel.get("disabled") and not cancel.get("aria-busy")
    ctx.eval("press('nv-nw-create'); press('nv-nw-create');")
    settle(ctx)
    assert len(_posts(ctx)) == 1, "a second Create while one is out sends nothing"


def test_a_failed_create_can_be_sent_again(ctx):
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "no-such-template"})
    _mount(ctx, [LOCAL])
    _refuse_create_with(ctx, env)
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert ctx.eval("textOf(nativeButton('nv-nw-create'))") == "Create" and ctx.eval("!nativeButton('nv-nw-create').props['aria-busy']")
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert len(_posts(ctx)) == 2


# ---- the states before there is a template ---------------------------------------------------------------------------------------------------------------


def test_a_failed_load_says_so_and_retries(ctx):
    status, env = envelope("GET", "/v1/workspace_templates/nope")
    ctx.eval(f"fail({json.dumps(KEY)}, {json.dumps(env)});")
    _mount(ctx)
    assert "Couldn't load workspace templates." in ctx.eval("textOf(MR.find('nv-nw-load-error'))")
    ctx.eval("MR.click('nv-nw-retry');")
    assert js(ctx, f"RES[{json.dumps(KEY)}].refetched") == 1
    assert js(ctx, "dialog().selector") is None


def test_no_templates_offers_to_create_one(ctx):
    _mount(ctx, [])
    assert ctx.eval("textOf(MR.find('nv-nw-empty'))").startswith("No workspace templates yet.")
    button = js(ctx, "MR.find('nv-nw-create-template').props")
    assert button["data-verb"] == "template.create" and ctx.eval("textOf(nativeButton('nv-nw-create-template'))") == "Create a template"
    assert js(ctx, "dialog().selector") is None
    assert js(ctx, "nativeButton('nv-nw-create').props.disabled") is True


def test_create_a_template_hands_the_new_one_back_selected(ctx):
    """Lead ruling 2: "Create a template" opens the template overlay OVER the dialog (the dialog stays, nothing navigates and no verb runs); the template
    it makes comes back selected, even before the refreshed list holds it, and stays selected when the list arrives with someone else's first."""
    ctx.eval("HANDLER = function (m, p, b) { if (m === 'POST' && p === '/workspace_templates') return Promise.resolve(Object.assign({ id: 'tpl-new' }, b));"
             " return Promise.resolve(b == null ? { ok: true } : b); };")
    _mount(ctx, [])
    ctx.eval("press('nv-nw-create-template');")
    assert ctx.eval("!!MR.find('wt-overlay') && !!MR.find('nv-overlay:new-workspace')")
    assert js(ctx, "FORMS['wt-overlay'].title") == "New workspace template"
    ctx.eval("setValue('ws-template-description', 'made in the dialog'); press('wt-save');")
    settle(ctx)
    assert [c["method"] + " " + c["path"] for c in js(ctx, "CALLS")] == ["POST /workspace_templates"]
    assert ctx.eval("MR.find('wt-overlay') === null"), "the template overlay closed"
    assert js(ctx, "OVERLAY_CLOSED") == 0 and js(ctx, "VERB_RUNS") == [], "the dialog stays; nothing leaves it"
    assert js(ctx, "TOASTS") == [["Template created: tpl-new", None]]
    assert js(ctx, "dialog().selector.value") == "tpl-new"
    assert ctx.eval("textOf(MR.find('nv-nw-summary'))") == "made in the dialog · Local on loc"
    assert js(ctx, f"RES[{json.dumps(KEY)}].refetched") >= 1, "the dialog asks for the templates again"
    ctx.eval(f"serve({json.dumps(KEY)}, {{ items: [{{ id: 'a-made-meanwhile', provider_id: 'loc', backend: {{ kind: 'local' }} }},"
             " { id: 'tpl-new', description: 'made in the dialog', provider_id: 'loc', backend: { kind: 'local' } }] }); MR.rerender();")
    assert js(ctx, "dialog().selector.value") == "tpl-new"
    _answer_create_with(ctx, {"id": "ws-new"})
    ctx.eval("press('nv-nw-create');")
    settle(ctx)
    assert _posts(ctx)[0]["body"] == {"template_id": "tpl-new"}


def test_cancelling_the_template_overlay_leaves_the_dialog_as_it_was(ctx):
    _mount(ctx, [])
    ctx.eval("press('nv-nw-create-template');")
    ctx.eval("press('wt-cancel');")
    assert ctx.eval("MR.find('wt-overlay') === null && !!MR.find('nv-nw-empty')")
    assert js(ctx, "OVERLAY_CLOSED") == 0 and js(ctx, "CALLS") == []


def test_cancel_closes_the_dialog(ctx):
    _mount(ctx, [LOCAL])
    ctx.eval("press('nv-nw-cancel');")
    assert js(ctx, "OVERLAY_CLOSED") == 1 and _posts(ctx) == []
