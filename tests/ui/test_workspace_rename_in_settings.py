"""Rename moves to the workspace's Settings, Config tab (platform revamp Plan 08, Task 14; lead ruling 4 of the plan; design 14.1 "nothing is lost").

Rename (``PATCH /v1/workspaces/{id}`` with ``{"name"}``) was offered only on the Workspaces page's cards, which Task 14 removes. ``WS_ConfigTab`` (the Config
tab of ``WorkspaceDetail``, the workspace's Settings overlay) shows the workspace's name with a Rename button that opens the same ``WS_RenameWorkspaceModal``;
a save refetches the row and the rail's workspace list (``nv-workspaces``) so the new name shows where the workspace is picked. Save says it is busy the kit's
way while the request is out, never natively disabled. The default workspace (prime) can be renamed: the server allows it, only its delete is refused.

The real ``shared.jsx`` (``Modal``, ``Btn``) and ``workspaces.jsx`` run here on the hook runtime (``tests/ui/_register_harness.py``), with the network and
``useMutation`` stubbed.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled

WORKSPACES_FILE = COMPONENTS / "workspaces.jsx"

STUBS = r"""
var REFETCHED = 0, INVALIDATES = [];
window.primerApi.useMutation = function (fn, opts) {
  var state = React.useState(false);
  var inv = JSON.stringify((opts && opts.invalidates) || []);
  if (INVALIDATES.indexOf(inv) < 0) INVALIDATES.push(inv);
  return {
    mutate: function (arg) {
      state[1](true);
      var p = fn(arg).then(function (answer) {
        state[1](false);
        if (opts && opts.onSuccess) opts.onSuccess(answer);
        return answer;
      }, function (err) {
        state[1](false);
        if (opts && opts.onError) opts.onError(err);
        throw err;
      });
      p.catch(function () {});
      return p;
    },
    loading: state[0]
  };
};
function ws(row) { return { data: row, loading: false, error: null, refetch: function () { REFETCHED += 1; } }; }
"""

ROW = {"id": "ws-1", "name": "Primer", "template_id": "local-default", "provider_id": "local", "created_at": "2026-10-20T10:00:00Z"}


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(WORKSPACES_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


def _mount(ctx, row: dict) -> None:
    ctx.eval(f"MR.mount(window.WS_ConfigTab, {{ wid: {json.dumps(row['id'])}, ws: ws({json.dumps(row)}), pushToast: window.primerApi.toastPush }});")


def test_the_config_tab_shows_the_name_and_a_rename_button(ctx):
    _mount(ctx, ROW)
    assert ctx.eval("textOf(MR.find('ws-config-name'))").startswith("Primer")
    assert ctx.eval("textOf(nativeButton('ws-config-rename'))") == "Rename"
    assert ctx.eval("MR.find('ws-rename-name') === null"), "the rename dialog opens on the button only"


def test_a_workspace_with_no_name_says_so(ctx):
    _mount(ctx, {**ROW, "name": None})
    assert ctx.eval("textOf(MR.find('ws-config-name'))").startswith("No name")


def test_rename_sends_the_patch_and_refreshes_the_row_and_the_rail(ctx):
    _mount(ctx, ROW)
    ctx.eval("press('ws-config-rename');")
    assert js(ctx, "MR.find('ws-rename-name').props.value") == "Primer"
    ctx.eval("setValue('ws-rename-name', 'Renamed'); press('ws-rename-save');")
    settle(ctx)
    assert [(c["method"], c["path"], c["body"]) for c in js(ctx, "CALLS")] == [("PATCH", "/workspaces/ws-1", {"name": "Renamed"})]
    assert js(ctx, "REFETCHED") == 1
    assert ctx.eval("MR.find('ws-rename-name') === null"), "the dialog closed"
    assert js(ctx, "TOASTS")[0]["title"] == "Workspace renamed"
    invalidated = [json.loads(i) for i in js(ctx, "INVALIDATES")]
    assert any("nv-workspaces" in i and "workspace-detail:ws-1" in i for i in invalidated), invalidated


def test_the_default_workspace_can_be_renamed(ctx):
    _mount(ctx, {**ROW, "is_default": True})
    ctx.eval("press('ws-config-rename'); setValue('ws-rename-name', 'prime'); press('ws-rename-save');")
    settle(ctx)
    assert [(c["method"], c["path"]) for c in js(ctx, "CALLS")] == [("PATCH", "/workspaces/ws-1")]


def test_save_is_busy_not_disabled_while_the_request_is_out(ctx):
    _mount(ctx, ROW)
    ctx.eval("press('ws-config-rename'); HANDLER = function () { return new Promise(function () {}); }; press('ws-rename-save');")
    settle(ctx)
    save = js(ctx, "nativeButton('ws-rename-save').props")
    assert save.get("aria-busy") == "true" and not save.get("disabled")
    cancel = js(ctx, "nativeButton('ws-rename-cancel').props")
    assert cancel.get("aria-disabled") == "true" and not cancel.get("disabled")


def test_a_refused_rename_keeps_the_dialog_open_and_says_why(ctx):
    env = {"type": "/errors/forbidden", "title": "Forbidden", "status": 403, "detail": "Only the workspace's owner may rename it"}
    _mount(ctx, ROW)
    ctx.eval("var ENV = " + json.dumps(env) + "; HANDLER = function () { return refuse(ENV); };")
    ctx.eval("press('ws-config-rename'); setValue('ws-rename-name', 'x'); press('ws-rename-save');")
    settle(ctx)
    assert ctx.eval("!!MR.find('ws-rename-name')") and js(ctx, "REFETCHED") == 0
    toast = js(ctx, "TOASTS")[0]
    assert toast["kind"] == "error" and toast["title"] == "Rename failed" and toast["detail"] == "Only the workspace's owner may rename it"
