"""The workspace template overlay (platform revamp Plan 08, Task 10; design 14.2): ``WT_TemplateOverlay`` and its files editor ``WT_FilesEditor``.

One kit overlay creates, edits and shows a template. "Runs on" at the centre top picks the provider (locked once the template exists) and swaps the
provider section: Kubernetes (image, size, disk, placement, More), Docker (image, size, More) or Local (a note). Files, environment, start-up commands and
an Advanced fold follow for every provider. What it must never do:

* drop a field it does not show (Review Focus 1): the PUT is the row it loaded with only the changed fields written;
* offer Save or Delete where the server would answer 403 (Review Focus 5): the built-in template and, for a non-admin, a template that holds admin-only
  settings open read-only with the reason;
* let a non-admin type into an admin-only control;
* hide a refusal: each one lands under the field its ``loc`` names, a folded field's fold opens, and the rest is the one line above the footer;
* turn a button natively disabled for the length of a request.

The context holds the REAL ``api.js``, ``shared.jsx``, ``shared/form-field.jsx``, the platform kit, ``workspaces/shared.jsx`` and ``templates.jsx``
(``tests/ui/_register_harness.py``), with the network, the console and ``useMutation`` stubbed. Every refusal is the real router's
(``tests/ui/_wt_envelopes.py``).
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled
from tests.ui._wt_envelopes import envelope
from tests.ui.test_wt_helpers import CHOICES, FULL_K8S

TEMPLATES_FILE = COMPONENTS / "workspaces" / "templates.jsx"
SHARED_FILE = COMPONENTS / "workspaces" / "shared.jsx"

STUBS = r"""
var ROLE = "admin";
var CLOSED = 0, SAVED = [];
function NV_useConsole() { return { role: ROLE, toast: function (msg, extra) { TOASTS.push([msg, extra || null]); } }; }
window.primerApi.useMutation = function (fn, opts) {
  var state = React.useState(false);
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
var LAST_OVERLAY = null;
var __PKFO = window.PK_FormOverlay;
window.PK_FormOverlay = function (p) { LAST_OVERLAY = p; return __PKFO(p); };
function overlayProps() { return LAST_OVERLAY; }
// What the server holds now, by path: a save reads the template again before it writes (round 2 of #886, item 2), and a GET answers from here.
var ROWS = {};
HANDLER = function (method, path, body) {
  if (method === "GET" && ROWS[path]) return Promise.resolve(JSON.parse(JSON.stringify(ROWS[path])));
  return Promise.resolve(body == null ? { ok: true } : JSON.parse(JSON.stringify(body)));
};
function onClose() { CLOSED += 1; }
function onSaved(row) { SAVED.push(row); }
// The error line of a field: the alert under the control or table with that test id (a kit field's, or a table's own line).
function errorUnder(testid) {
  var row = fieldRow("wt-overlay", testid);
  if (row) { var hit = row.filter(function (c) { return c.role === "alert"; })[0]; return hit ? hit.text : null; }
  var line = MR.find(testid + "-error");
  return line ? textOf(line) : null;
}
function helpUnder(testid) {
  var row = fieldRow("wt-overlay", testid) || [];
  var hit = row.filter(function (c) { return c.className === "field-help" && c.role !== "alert"; })[0];
  return hit ? hit.text : null;
}
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval("serve('ws:provider-choices', { items: " + json.dumps([CHOICES["dock"], CHOICES["k3s"], CHOICES["loc"]]) + " });")
    try:
        yield c
    finally:
        c.close()


def _stored(ctx, row: dict) -> None:
    """The row the server holds now (what the save's GET reads)."""
    ctx.eval(f"ROWS[{json.dumps('/workspace_templates/' + row['id'])}] = {json.dumps(row)};")


def _mount(ctx, props: str, role: str = "admin") -> None:
    ctx.eval(f"ROLE = {json.dumps(role)}; MR.mount(window.WT_TemplateOverlay, Object.assign({{ onClose: onClose, onSaved: onSaved }}, {props}));")


def _calls(ctx, method: str) -> list:
    return [c for c in js(ctx, "CALLS") if c["method"] == method]


def _pick(ctx, provider: str) -> None:
    ctx.eval(f"overlayProps().selector.onChange({json.dumps(provider)}); MR.rerender();")


def _open_fold(ctx, testid: str) -> None:
    ctx.eval(f"MR.click({json.dumps(testid + ':toggle')});")


# ---- Review Focus 1 -----------------------------------------------------------------------------------------------------------------------------------


def test_edit_save_puts_the_row_back_with_only_the_description_changed(ctx):
    _stored(ctx, FULL_K8S)
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    assert js(ctx, "overlayProps().title") == "Edit workspace template"
    ctx.eval("setValue('ws-template-description', 'changed'); press('wt-save');")
    settle(ctx)
    puts = _calls(ctx, "PUT")
    assert len(puts) == 1 and puts[0]["path"] == "/workspace_templates/k8s-full"
    assert puts[0]["body"] == {**FULL_K8S, "description": "changed"}
    assert js(ctx, "TOASTS") == [["Template saved: k8s-full", None]]
    assert js(ctx, "SAVED")[0]["id"] == "k8s-full" and js(ctx, "CLOSED") == 1


def test_an_edit_by_id_loads_the_row_and_puts_it_back(ctx):
    ctx.eval("serve('ws:template:k8s-full', " + json.dumps(FULL_K8S) + ");")
    _stored(ctx, FULL_K8S)
    _mount(ctx, "{ templateId: 'k8s-full' }")
    assert "ws:template:k8s-full" in js(ctx, "KEYS")
    ctx.eval("setValue('wt-cpu', '3'); press('wt-save');")
    settle(ctx)
    body = _calls(ctx, "PUT")[0]["body"]
    assert body == {**FULL_K8S, "resources": {**FULL_K8S["resources"], "cpu_cores": 3}}


def test_a_save_reads_the_template_again_so_a_concurrent_edit_survives(ctx):
    """Round 2 of #886, item 2 (the reviewer's probe): another admin raises the memory and sets the storage class while I edit the description. The
    server's PUT replaces the whole row, so a save built on the row I opened would put their values back; the save reads the row again and writes only
    what I changed onto it."""
    theirs = {**FULL_K8S, "resources": {**FULL_K8S["resources"], "memory_bytes": 8 * 2**30}, "backend": {**FULL_K8S["backend"], "storage_class": "fast"}}
    _stored(ctx, theirs)
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    ctx.eval("setValue('ws-template-description', 'mine'); press('wt-save');")
    settle(ctx)
    assert [(c["method"], c["path"]) for c in js(ctx, "CALLS")] == [("GET", "/workspace_templates/k8s-full"), ("PUT", "/workspace_templates/k8s-full")]
    assert _calls(ctx, "PUT")[0]["body"] == {**theirs, "description": "mine"}


def test_a_template_deleted_meanwhile_is_a_refusal_not_a_write(ctx):
    status, gone = envelope("GET", "/v1/workspace_templates/k8s-full")
    assert status == 404
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    ctx.eval("var GONE = " + json.dumps(gone) + "; HANDLER = function (m) { return m === 'GET' ? refuse(GONE) : Promise.resolve({}); };")
    ctx.eval("setValue('ws-template-description', 'mine'); press('wt-save');")
    settle(ctx)
    assert _calls(ctx, "PUT") == [] and js(ctx, "CLOSED") == 0
    assert ctx.eval("textOf(MR.find('wt-error'))") == js(ctx, "window.primerApi.readRefusal(new window.primerApi.ApiError(GONE)).message")


def test_an_odd_stored_size_does_not_block_the_save_of_another_field(ctx):
    """Round 2 of #886, item 5: a 1Mi disk the server took shows as 0 GiB; the overlay checks only what the person changed, and the 1Mi goes back as stored."""
    row = {**FULL_K8S, "backend": {**FULL_K8S["backend"], "pvc_size": "1Mi"}, "resources": {**FULL_K8S["resources"], "cpu_cores": 1e-7}}
    _stored(ctx, row)
    _mount(ctx, "{ row: " + json.dumps(row) + " }")
    assert js(ctx, "MR.find('wt-disk').props.value") == "0"
    ctx.eval("setValue('ws-template-description', 'changed'); press('wt-save');")
    settle(ctx)
    assert _calls(ctx, "PUT")[0]["body"] == {**row, "description": "changed"}


def test_the_edit_says_existing_workspaces_keep_their_copy(ctx):
    """Template snapshots (#829): a workspace keeps the template it was made from, so an edit changes only the workspaces made after it."""
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    note = ctx.eval("textOf(MR.find('wt-snapshot-note'))")
    assert note == "Changes apply to workspaces made from now on; existing workspaces keep the copy of the template they were made from."
    _mount(ctx, "{ create: true }")
    assert ctx.eval("MR.find('wt-snapshot-note') === null")


# ---- create --------------------------------------------------------------------------------------------------------------------------------------------


def test_a_blank_description_is_refused_under_the_field(ctx):
    _mount(ctx, "{ create: true }")
    assert js(ctx, "overlayProps().title") == "New workspace template"
    assert js(ctx, "errorUnder('ws-template-description')") is None, "no error before the person tries to save"
    ctx.eval("press('wt-save');")
    settle(ctx)
    assert _calls(ctx, "POST") == []
    assert js(ctx, "errorUnder('ws-template-description')") == "Enter a description"
    ctx.eval("setValue('ws-template-description', 'Docker dev');")
    assert js(ctx, "errorUnder('ws-template-description')") is None, "the error follows the field once it is fixed"


def test_create_prefills_the_docker_image_and_says_what_it_must_contain(ctx):
    _mount(ctx, "{ create: true }")
    assert js(ctx, "overlayProps().selector.value") == "dock", "the first provider, by id"
    assert js(ctx, "MR.find('ws-template-image').props.value") == "primer/workspace-runtime:1.1"
    assert js(ctx, "helpUnder('ws-template-image')") == "The built-in runtime image is filled in. A custom image must contain the primer workspace runtime."
    ctx.eval("setValue('ws-template-description', 'Docker dev'); setValue('ws-template-id', 'docker-dev'); press('wt-save');")
    settle(ctx)
    post = _calls(ctx, "POST")
    assert len(post) == 1 and post[0]["path"] == "/workspace_templates"
    assert post[0]["body"]["id"] == "docker-dev" and post[0]["body"]["backend"]["image"] == "primer/workspace-runtime:1.1"
    assert js(ctx, "TOASTS") == [["Template created: docker-dev", None]] and js(ctx, "CLOSED") == 1


def test_create_prefills_the_kubernetes_runtime_command(ctx):
    _mount(ctx, "{ create: true }")
    _pick(ctx, "k3s")
    assert js(ctx, "MR.find('ws-template-image').props.value") == "", "the Docker image does not follow to Kubernetes"
    assert js(ctx, "helpUnder('ws-template-image')") == "Must include the primer workspace runtime"
    _open_fold(ctx, "wt-k8s-more")
    assert js(ctx, "MR.find('wt-command').props.value") == "python\n-m\nprimer_runtime.server"
    ctx.eval("setValue('ws-template-description', 'k'); setValue('ws-template-image', 'rt:1.4'); press('wt-save');")
    settle(ctx)
    body = _calls(ctx, "POST")[0]["body"]
    assert body["backend"]["entrypoint"] == ["python", "-m", "primer_runtime.server"] and body["backend"]["pvc_size"] == "10Gi"


def test_runs_on_swaps_the_section(ctx):
    _mount(ctx, "{ create: true }")
    options = js(ctx, "overlayProps().selector.options")
    assert [(o["value"], o["label"]) for o in options] == [("dock", "dock · Docker"), ("k3s", "k3s · Kubernetes"), ("loc", "loc · Local")]
    assert js(ctx, "overlayProps().selector.prefix") == "Runs on" and js(ctx, "overlayProps().selector.testid") == "ws-template-provider"
    assert js(ctx, "overlayProps().selector.locked") is False
    _pick(ctx, "k3s")
    assert ctx.eval("!!MR.find('wt-k8s') && !MR.find('wt-docker') && !MR.find('wt-local')")
    _pick(ctx, "dock")
    assert ctx.eval("!!MR.find('wt-docker') && !MR.find('wt-k8s')")
    _pick(ctx, "loc")
    assert ctx.eval("textOf(MR.find('wt-local'))") == "Runs on the server itself, in the provider's root folder. Local workspaces have no size settings."
    assert ctx.eval("MR.find('wt-cpu') === null")


def test_the_provider_is_locked_once_the_template_exists(ctx):
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    assert js(ctx, "overlayProps().selector.locked") is True and js(ctx, "overlayProps().selector.value") == "k3s"
    assert js(ctx, "MR.find('ws-template-id').props.disabled") is True


# ---- Review Focus 5 -----------------------------------------------------------------------------------------------------------------------------------


def test_view_mode_for_the_built_in_template(ctx):
    _mount(ctx, "{ row: { id: 'local-default', description: 'Default', provider_id: 'local', backend: { kind: 'local' } } }")
    assert js(ctx, "overlayProps().title") == "Workspace template"
    assert ctx.eval("MR.find('wt-save') === null") and ctx.eval("!!MR.find('wt-close')")
    assert ctx.eval("textOf(MR.find('wt-readonly-note'))") == "Built in: this template ships with primer and cannot be changed."
    assert js(ctx, "MR.find('ws-template-description').props.readOnly") is True
    ctx.eval("MR.click('wt-close');")
    assert js(ctx, "CLOSED") == 1


def test_view_mode_for_a_user_on_an_admin_held_template(ctx):
    row = {**FULL_K8S, "backend": {**FULL_K8S["backend"], "service_account_name": None, "pod_overrides": None, "extra_volumes": [], "extra_volume_mounts": []}}
    _mount(ctx, "{ row: " + json.dumps(row) + " }", role="user")
    assert ctx.eval("MR.find('wt-save') === null")
    assert ctx.eval("textOf(MR.find('wt-readonly-note'))") == (
        "This template holds admin-only settings (backend.tolerations). Only an admin can change or delete it."
    )
    assert ctx.eval("MR.find('wt-tolerations:add') === null") and ctx.eval("MR.find('wt-env:add') === null")
    _mount(ctx, "{ row: " + json.dumps(row) + " }", role="admin")
    assert ctx.eval("!!MR.find('wt-save')"), "an admin edits it"


def test_a_user_cannot_touch_admin_only_fields(ctx):
    _mount(ctx, "{ create: true }", role="user")
    _pick(ctx, "k3s")
    assert js(ctx, "MR.find('wt-service-account').props.disabled") is True
    assert js(ctx, "MR.find('wt-gpu-resource').props.disabled") is True
    assert ctx.eval("!!MR.find('wt-tolerations') && MR.find('wt-tolerations:add') === null"), "the tolerations table is read-only"
    assert js(ctx, "helpUnder('wt-service-account')") == "Admin only"
    _open_fold(ctx, "wt-k8s-more")
    for testid in ("wt-extra-volumes", "wt-extra-volume-mounts", "wt-pod-dns", "wt-pod-grace"):
        assert js(ctx, f"MR.find('{testid}').props.disabled") is True, testid
    assert js(ctx, "MR.find('wt-pod-share-pid:on').props.disabled") is True and js(ctx, "MR.find('wt-pod-restart:Never').props.disabled") is True
    assert ctx.eval("MR.find('ws-template-admin-only-note') === null")
    assert ctx.eval("!MR.find('wt-command').props.disabled") is True, "the command is the user's own"
    _pick(ctx, "dock")
    _open_fold(ctx, "wt-docker-more")
    assert ctx.eval("MR.find('wt-extra-mounts:add') === null")
    assert ctx.eval("MR.find('wt-files-add') !== null")
    ctx.eval("MR.click('wt-files-add');")
    assert js(ctx, "MR.findProp('testid', 'wt-files-source:0').props.options.map(function (o) { return o.label; })") == ["Text", "URL", "Document"]


def test_an_admin_sees_the_admin_note_in_each_more_fold(ctx):
    _mount(ctx, "{ create: true }")
    _pick(ctx, "k3s")
    _open_fold(ctx, "wt-k8s-more")
    assert ctx.eval("textOf(MR.find('ws-template-admin-only-note'))").endswith(js(ctx, "WT_ADMIN_ONLY_NOTE"))
    assert ctx.eval("!MR.find('wt-service-account').props.disabled") is True
    ctx.eval("MR.click('wt-files-add');")
    assert "Secret" in js(ctx, "MR.findProp('testid', 'wt-files-source:0').props.options.map(function (o) { return o.label; })")


# ---- refusals -----------------------------------------------------------------------------------------------------------------------------------------


def _sendable(row: dict) -> dict:
    """``row`` without the key from the future, which the real model refuses: the refusal a test needs is the one about the field it changes."""
    out = {k: v for k, v in row.items() if k != "x_future"}
    out["backend"] = {k: v for k, v in row["backend"].items() if k != "x_future"}
    return out


def _save_refused(ctx, status_envelope) -> None:
    ctx.eval("var ENVELOPE = " + json.dumps(status_envelope[1]) + "; HANDLER = function () { return refuse(ENVELOPE); };")
    ctx.eval("press('wt-save');")
    settle(ctx)


def test_server_field_errors_land_under_their_fields(ctx):
    bad = envelope("PUT", "/v1/workspace_templates/k8s-full", _sendable({**FULL_K8S, "backend": {**FULL_K8S["backend"], "node_selector": {"bad key": "x"}}}))
    assert bad[0] == 422 and len(bad[1]["extensions"]["errors"]) == 1
    message = bad[1]["extensions"]["errors"][0]["msg"].removeprefix("Value error, ")
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    _save_refused(ctx, bad)
    assert js(ctx, "errorUnder('wt-node-selector')") == message
    assert ctx.eval("MR.find('wt-error') === null")
    forbidden = envelope("POST", "/v1/workspace_templates", {"id": "t-y", "description": "d", "provider_id": "k3s",
                                                              "backend": {"kind": "kubernetes", "image": "rt", "tolerations": [{"operator": "Exists"}]}}, role="user")
    assert forbidden[0] == 403
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    _save_refused(ctx, forbidden)
    line = ctx.eval("textOf(MR.find('wt-error'))")
    assert "backend.tolerations" in line and js(ctx, "MR.find('wt-error').props.role") == "alert"
    assert js(ctx, "CLOSED") == 0, "a refused save keeps the overlay open"


def test_a_refusal_in_a_folded_field_opens_its_fold(ctx):
    row = {**FULL_K8S, "state_path": "/etc"}
    bad = envelope("PUT", "/v1/workspace_templates/k8s-full", _sendable(row))
    assert bad[0] == 422 and [e["loc"][-1] for e in bad[1]["extensions"]["errors"]] == ["state_path"]
    _mount(ctx, "{ row: " + json.dumps(row) + " }")
    assert ctx.eval("MR.find('wt-state-path') === null"), "Advanced starts folded"
    _save_refused(ctx, bad)
    assert js(ctx, "errorUnder('wt-state-path')") == bad[1]["extensions"]["errors"][0]["msg"].removeprefix("Value error, ")


def test_a_refusal_no_field_shows_is_one_line_with_its_field(ctx):
    bad = envelope("POST", "/v1/workspace_templates", {"id": "t-z", "description": "d", "provider_id": "dock",
                                                       "backend": {"kind": "container"}, "resources": {"cpu_cores": 0}})
    assert bad[0] == 422
    _mount(ctx, "{ create: true }")
    ctx.eval("setValue('ws-template-description', 'd');")
    _save_refused(ctx, bad)
    assert ctx.eval("textOf(MR.find('wt-error'))") == "resources.cpu_cores: " + bad[1]["extensions"]["errors"][0]["msg"], (
        "a Docker template's size is backend.cpu_cores: the stored resources one has no field here"
    )


def test_the_overlay_buttons_are_never_disabled_for_a_request(ctx):
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    ctx.eval("HANDLER = function () { return new Promise(function () {}); }; setValue('ws-template-description', 'x'); press('wt-save');")
    settle(ctx)
    save = js(ctx, "nativeButton('wt-save').props")
    assert save.get("aria-busy") == "true" and not save.get("disabled")
    assert ctx.eval("textOf(nativeButton('wt-save'))") == "Saving…"
    cancel = js(ctx, "nativeButton('wt-cancel').props")
    assert cancel.get("aria-disabled") == "true" and not cancel.get("disabled")


# ---- states -------------------------------------------------------------------------------------------------------------------------------------------


def test_loading_gone_and_load_error_states(ctx):
    """The overlay's states have test ids of their own: the page's list states (wt-loading ...) can be on screen under it."""
    _mount(ctx, "{ templateId: 'later' }")
    assert ctx.eval("textOf(MR.find('wt-overlay-loading'))") == "Loading the template…" and ctx.eval("MR.find('wt-save') === null")
    ctx.eval("fail('ws:template:gone', { type: '/errors/not-found', title: 'Not Found', status: 404, detail: 'no such template' });")
    _mount(ctx, "{ templateId: 'gone' }")
    assert ctx.eval("textOf(MR.find('wt-gone'))") == "This template no longer exists." and ctx.eval("!!MR.find('wt-close')")
    ctx.eval("fail('ws:template:broken', { type: '/errors/internal', title: 'Internal Server Error', status: 500, detail: 'boom' });")
    _mount(ctx, "{ templateId: 'broken' }")
    assert "boom" in ctx.eval("textOf(MR.find('wt-load-error'))")
    ctx.eval("MR.click('wt-load-retry');")
    assert js(ctx, "RES['ws:template:broken'].refetched") == 1


def test_no_providers_and_a_provider_error(ctx):
    ctx.eval("serve('ws:provider-choices', { items: [] });")
    _mount(ctx, "{ create: true }")
    assert ctx.eval("textOf(MR.find('wt-no-providers'))") == "No workspace providers yet. An admin registers one under Providers › Workspace."
    assert ctx.eval("MR.find('wt-save') === null")
    ctx.eval("fail('ws:provider-choices', { type: '/errors/internal', title: 'Internal Server Error', status: 500, detail: 'down' });")
    _mount(ctx, "{ create: true }")
    assert "Couldn't load the workspace providers." in ctx.eval("textOf(MR.find('wt-providers-error'))")
    ctx.eval("MR.click('wt-providers-retry');")
    assert js(ctx, "RES['ws:provider-choices'].refetched") == 1


def test_an_edit_needs_no_provider_list_to_show_its_provider(ctx):
    """A template whose provider is gone (or a list that failed) still opens: the locked selector names the provider the row holds."""
    ctx.eval("serve('ws:provider-choices', { items: [] });")
    _mount(ctx, "{ row: " + json.dumps(FULL_K8S) + " }")
    assert js(ctx, "overlayProps().selector.options") == [{"value": "k3s", "label": "k3s · Kubernetes", "iconPath": js(ctx, "WT_KIND_ICONS.kubernetes")}]
    assert ctx.eval("!!MR.find('wt-save')")


# ---- the files editor ---------------------------------------------------------------------------------------------------------------------------------

URL_FILE = {"path": "b.bin", "source": {"kind": "url", "url": "https://u:**********@files.example/x", "sha256": "ab" * 32}, "mode": "0600"}


def _files(ctx, value: list, *, is_admin: bool = True, read_only: bool = False) -> None:
    ctx.eval("var FILES_SEEN = [];")
    ctx.eval(f"MR.mount(window.WT_FilesEditor, {{ value: {json.dumps(value)}, isAdmin: {json.dumps(is_admin)}, readOnly: {json.dumps(read_only)},"
             " testid: 'f', onChange: function (next) { FILES_SEEN.push(next); } });")


def test_files_editor_keeps_what_it_does_not_show(ctx):
    _files(ctx, [URL_FILE])
    assert js(ctx, "MR.find('f-url:0').props.value") == URL_FILE["source"]["url"] and js(ctx, "MR.find('f-sha256:0').props.value") == "ab" * 32
    ctx.eval("MR.find('f-path:0').props.onChange({ target: { value: 'c.bin' } });")
    assert js(ctx, "FILES_SEEN[0]") == [{**URL_FILE, "path": "c.bin"}]
    ctx.eval("MR.findProp('testid', 'f-source:0').props.onChange('inline');")
    assert js(ctx, "FILES_SEEN[1]") == [{**URL_FILE, "source": {"kind": "inline", "content": ""}}]
    ctx.eval("MR.find('f-mode:0').props.onChange({ target: { value: '' } });")
    assert js(ctx, "FILES_SEEN[2]")[0]["mode"] is None


def test_files_editor_adds_and_removes_rows_and_draws_each_source(ctx):
    rows = [{"path": "a", "source": {"kind": "inline", "content": "hi"}, "mode": None},
            {"path": "d", "source": {"kind": "document", "collection_id": "c", "document_id": "x"}, "mode": None},
            {"path": "s", "source": {"kind": "secret", "name": "PRIMER_SECRET_X"}, "mode": None}]
    _files(ctx, rows, is_admin=False)
    assert js(ctx, "MR.find('f-content:0').props.value") == "hi"
    assert js(ctx, "[MR.find('f-collection:1').props.value, MR.find('f-document:1').props.value]") == ["c", "x"]
    assert js(ctx, "MR.find('f-secret:2').props.value") == "PRIMER_SECRET_X"
    assert "Secret" in js(ctx, "MR.findProp('testid', 'f-source:2').props.options.map(function (o) { return o.label; })"), "a row that holds a secret shows it"
    assert "Secret" not in js(ctx, "MR.findProp('testid', 'f-source:0').props.options.map(function (o) { return o.label; })")
    ctx.eval("MR.click('f-add');")
    assert js(ctx, "FILES_SEEN[0]")[-1] == {"path": "", "source": {"kind": "inline", "content": ""}, "mode": None}
    ctx.eval("MR.click('f-remove:1');")
    assert [f["path"] for f in js(ctx, "FILES_SEEN[1]")] == ["a", "s"]
    _files(ctx, rows, read_only=True)
    assert ctx.eval("MR.find('f-add') === null && MR.find('f-remove:0') === null")
    assert js(ctx, "MR.find('f-path:0').props.readOnly") is True and js(ctx, "MR.findProp('testid', 'f-source:0').props.disabled") is True
