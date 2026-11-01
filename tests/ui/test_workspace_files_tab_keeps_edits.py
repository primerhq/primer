"""The workspace Files tab saves and creates with a check (UI-M4).

``WS_FilesTab`` (``ui/components/workspaces.jsx``) saved with a bare PUT -
no etag - so a save landing after an agent's write silently replaced the
agent's bytes; and "New file" PUT to the given path straight away, so a path
that already exists was replaced with the toast still saying "File created".

Now a save carries ``&etag=`` from the content read, and a 412 keeps the
draft and offers the console editor's choice (Reload takes the disk version;
Overwrite re-PUTs without the etag). A new file reads the path first and
asks through ``confirmDialog`` when a file is there; a missing path (404)
still creates directly.

MiniRacer on the real file with a stubbed ``window.primerApi``: a FILES
store behind the reads, deferred PUTs the test settles, a resource cache
whose entries reload through the component's own fetcher, and a
``confirmDialog`` the test answers.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

WORKSPACES = Path(__file__).resolve().parents[2] / "ui" / "components" / "workspaces.jsx"

PRELUDE = r"""
globalThis.Icon = function () { return null; };
globalThis.Btn = function (props) {
  return React.createElement("button", {
    "data-testid": props["data-testid"], onClick: props.onClick,
    disabled: props.disabled || !!props.busy, title: props.title
  }, props.children);
};
globalThis.Modal = function (props) {
  return React.createElement("div", { "data-testid": props.testid || "ws-modal" },
    props.children, props.footer);
};
globalThis.Banner = function (props) { return React.createElement("div", null, props.children); };
window.location = { origin: "http://x", search: "", hash: "", pathname: "/console/" };
window.requestAnimationFrame = function () { return 0; };
window.cancelAnimationFrame = function () {};
var FILES = { "a.txt": { content: "old", etag: "e1" } };
var REQS = [];
var PUTS = [];
var TOASTS = [];
var CONFIRM = { calls: [], pending: null };
window.confirmDialog = function (cfg) {
  CONFIRM.calls.push(cfg);
  return new Promise(function (res) { CONFIRM.pending = function (a) { res(a); }; });
};
var RES = {};
function __pathOf(url) {
  var m = url.split("path=")[1];
  return decodeURIComponent(m.split("&")[0]);
}
window.primerApi = {
  apiFetch: function (method, url, payload, opts) {
    REQS.push({ method: method, url: url });
    var path = __pathOf(url);
    if (method === "GET" && url.indexOf("/files/read?") !== -1) {
      var f = FILES[path];
      if (f) return Promise.resolve({
        path: path, encoding: "text", content: f.content,
        size_bytes: f.content.length, etag: f.etag
      });
      return Promise.reject({ status: 404, detail: "no such file" });
    }
    if (method === "GET" && url.indexOf("/files?path=") !== -1) {
      return Promise.resolve({ items: [{ path: "a.txt", kind: "file", size_bytes: 3 }] });
    }
    if (method === "PUT") {
      return new Promise(function (res, rej) {
        PUTS.push({ path: path, url: url, payload: payload, res: res, rej: rej });
      });
    }
    return Promise.resolve({});
  },
  useResource: function (key, fetcher) {
    if (!RES[key]) {
      RES[key] = {
        data: undefined, error: null, loading: false, degraded: false,
        refetch: function () {
          return fetcher(null).then(
            function (d) { RES[key].data = d; },
            function (e) { RES[key].error = e; }
          );
        }
      };
    }
    return RES[key];
  },
  useMutation: function (factory, opts) {
    var st = { loading: false };
    return {
      loading: st.loading,
      mutate: function (vars) {
        st.loading = true;
        Promise.resolve(factory(vars)).then(
          function (res) { st.loading = false; if (opts && opts.onSuccess) opts.onSuccess(res); },
          function (err) { st.loading = false; if (opts && opts.onError) opts.onError(err); }
        );
      }
    };
  }
};
RES["workspace-files:W1:"] = {
  data: { items: [{ path: "a.txt", kind: "file", size_bytes: 3 }] },
  error: null, loading: false, refetch: function () {}
};
"""

DRIVER = r"""
var CONTENT_KEY = "workspace-file-content:W1:a.txt";
function __selectFile() {
  MR.click("ws-file-row:a.txt");
  MR.rerender();
  RES[CONTENT_KEY].refetch();
}
// The content read's promise resolves between evals; the rerender the test
// does next sees it and the draft sync.
function __readLands() { MR.rerender(); }
function __edit() { MR.click("ws-file-edit-edit"); }
function __type(v) { MR.find("ws-file-edit").props.onChange({ target: { value: v } }); MR.rerender(); }
function __value() { return MR.find("ws-file-edit").props.value; }
function __save() { MR.click("ws-file-edit-save"); }
function __settlePut(i, err) {
  // The mutation's then handlers are microtasks that drain after this
  // eval; the separate rerender eval the test does next sees their state.
  var p = PUTS[i];
  if (err) { p.rej(err); } else { p.res({ ok: true }); }
}
function __openCreate(path) {
  MR.click("ws-new-file");
  MR.rerender();
  MR.find("ws-create-path").props.onChange({ target: { value: path } });
  MR.rerender();
  MR.click("ws-create-submit");
}
function __confirm(a) { CONFIRM.pending(a); }
function __lastToast() { return TOASTS[TOASTS.length - 1]; }
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _settle(ctx, i: int, err: bool = False) -> None:
    ctx.eval(f"__settlePut({i}" + (", { status: 412, detail: 'etag mismatch' })" if err else ")"))
    ctx.eval("MR.rerender();")


def _ctx():
    ctx = mini_react_context(transpile(WORKSPACES), PRELUDE)
    ctx.eval(DRIVER)
    ctx.eval('MR.mount(WS_FilesTab, { wid: "W1", pushToast: function (t) { TOASTS.push(t); } });')
    return ctx


def _select_and_edit(ctx, text: str) -> None:
    ctx.eval("__selectFile();")
    ctx.eval("__readLands();")
    ctx.eval("__edit();")
    ctx.eval(f'__type("{text}");')


def test_a_save_sends_the_read_etag() -> None:
    # The PUT must carry the etag the content read returned: the server
    # refuses a write whose etag no longer matches the file.
    ctx = _ctx()
    try:
        _select_and_edit(ctx, "old, edited")
        ctx.eval("__save();")
        assert len(_js(ctx, "PUTS")) == 1
        assert "&etag=e1" in _js(ctx, "PUTS")[0]["url"], "the save PUT did not carry the read's etag"
        _settle(ctx, 0)
        assert _js(ctx, "__lastToast()")["title"] == "File saved"
    finally:
        ctx.close()


def test_a_412_keeps_the_draft_and_reload_takes_the_disk_version() -> None:
    # A 412 (the file moved on disk since the read) keeps the draft and
    # offers the choice; Reload takes the disk version.
    ctx = _ctx()
    try:
        _select_and_edit(ctx, "old, edited")
        ctx.eval("__save();")
        _settle(ctx, 0, err=True)
        assert ctx.eval('!!MR.find("ws-file-conflict")'), "no conflict choice after a 412"
        assert _js(ctx, "__value()") == "old, edited", "the draft was dropped on a 412"
        ctx.eval('FILES["a.txt"] = { content: "agent", etag: "e2" };')
        ctx.eval('MR.click("ws-file-conflict-reload");')
        ctx.eval("__readLands();")
        ctx.eval("__edit();")
        assert _js(ctx, "__value()") == "agent", "Reload did not take the disk version"
    finally:
        ctx.close()


def test_overwrite_puts_without_the_etag_and_clears_the_banner() -> None:
    # Overwrite re-PUTs the draft with no etag; its success clears the
    # banner and toasts.
    ctx = _ctx()
    try:
        _select_and_edit(ctx, "mine")
        ctx.eval("__save();")
        _settle(ctx, 0, err=True)
        assert ctx.eval('!!MR.find("ws-file-conflict")')
        ctx.eval('MR.click("ws-file-conflict-overwrite");')
        assert len(_js(ctx, "PUTS")) == 2
        assert "&etag=" not in _js(ctx, "PUTS")[1]["url"], "the overwrite PUT still carried the stale etag"
        assert _js(ctx, "PUTS")[1]["payload"]["content"] == "mine"
        _settle(ctx, 1)
        assert not ctx.eval('!!MR.find("ws-file-conflict")')
        assert _js(ctx, "__lastToast()")["title"] == "File saved"
    finally:
        ctx.close()


def test_a_create_over_an_existing_path_confirms_before_the_put() -> None:
    # The path exists: the create asks through confirmDialog, and the PUT
    # leaves only after the operator confirms.
    ctx = _ctx()
    try:
        ctx.eval('__openCreate("a.txt");')
        assert len(_js(ctx, "PUTS")) == 0, "a PUT left before the operator confirmed the overwrite"
        assert len(_js(ctx, "CONFIRM.calls")) == 1
        ctx.eval("__confirm(true);")
        assert len(_js(ctx, "PUTS")) == 1
        assert _js(ctx, "PUTS")[0]["path"] == "a.txt"
        _settle(ctx, 0)
        assert _js(ctx, "__lastToast()")["title"] == "File created"
    finally:
        ctx.close()


def test_a_create_for_a_missing_path_puts_without_asking() -> None:
    # Characterization: a 404 read means the path is free and the create
    # goes out directly, no dialog.
    ctx = _ctx()
    try:
        ctx.eval('__openCreate("new.txt");')
        assert len(_js(ctx, "CONFIRM.calls")) == 0, "a missing path asked for a confirmation"
        assert len(_js(ctx, "PUTS")) == 1
        assert _js(ctx, "PUTS")[0]["path"] == "new.txt"
        _settle(ctx, 0)
        assert _js(ctx, "__lastToast()")["title"] == "File created"
    finally:
        ctx.close()
