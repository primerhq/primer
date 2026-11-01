"""The python toolset editor keeps edits typed while a save is out (UI-M10).

``PythonToolsetEditor`` (``ui/components/toolsets/python-editor.jsx``) cleared
its draft the moment the save PUT resolved, so keystrokes typed while the
request was out were thrown away and the editor fell back to the stored
source, which is the pre-save body until the invalidation refetch lands.

Now the draft clears only when the refetched stored source catches up to it
(a ``useEffect`` on ``[draft, stored]``): nothing more typed means the caught
up refetch ends the dirty state; anything typed during the save stays.

MiniRacer on the real file with a stubbed ``window.primerApi`` (deferred PUTs
the test settles, a detail read the test re-lands to model the refetch) and a
``window.PY_CodeEditor`` seam the real CodeMirror module occupies in the
browser (a textarea the test drives).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

PY_EDITOR = Path(__file__).resolve().parents[2] / "ui" / "components" / "toolsets" / "python-editor.jsx"

PRELUDE = r"""
globalThis.Icon = function () { return null; };
globalThis.Btn = function (props) {
  return React.createElement("button", {
    "data-testid": props["data-testid"], onClick: props.onClick,
    disabled: props.disabled || !!props.busy
  }, props.children);
};
globalThis.Banner = function () { return null; };
globalThis.CapabilityBadges = function () { return null; };
globalThis.setTimeout = function () { return 0; };
globalThis.clearTimeout = function () {};
window.PY_SCAFFOLDS = [];
// The seam the CodeMirror module occupies: a textarea the test drives.
window.PY_CodeEditor = function (props) {
  return React.createElement("textarea", {
    "data-testid": "python-source", value: props.value,
    onChange: function (e) { props.onChange(e.target.value); }
  });
};
var DETAIL = { data: { id: "ts1", config: { source: "src0", source_version: 2 } }, loading: false, error: null };
var RUNTIME = { data: {}, loading: false, error: null };
var PUTS = [];
var TOASTS = [];
window.primerApi = {
  useResource: function (key) {
    return key.indexOf("toolset-runtime:") === 0 ? RUNTIME : DETAIL;
  },
  useEscape: function () {},
  apiFetch: function (method, url, payload) {
    if (method === "PUT") {
      var entry = { url: url, payload: payload };
      PUTS.push(entry);
      return new Promise(function (res) { entry.res = res; });
    }
    return Promise.resolve({});
  },
  useMutation: function (factory, opts) {
    var st = { loading: false };
    return {
      loading: st.loading,
      mutate: function (vars) {
        st.loading = true;
        Promise.resolve(factory(vars)).then(
          function (r) { st.loading = false; if (opts && opts.onSuccess) opts.onSuccess(r); },
          function (e) { st.loading = false; if (opts && opts.onError) opts.onError(e); }
        );
      }
    };
  }
};
"""

DRIVER = r"""
function __type(v) { MR.find("python-source").props.onChange({ target: { value: v } }); MR.rerender(); }
function __value() { return MR.find("python-source").props.value; }
function __canSave() { return !MR.find("python-save").props.disabled; }
function __save() { MR.click("python-save"); }
function __settlePut(i) { PUTS[i].res({}); }
function __land(source) {
  // Model the invalidation refetch: the detail read now holds this body.
  DETAIL.data = { id: "ts1", config: { source: source, source_version: 3 } };
  MR.rerender();
}
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    ctx = mini_react_context(transpile(PY_EDITOR), PRELUDE)
    ctx.eval(DRIVER)
    ctx.eval(
        'MR.mount(PythonToolsetEditor, { toolsetId: "ts1", '
        'pushToast: function (t) { TOASTS.push(t); } });'
    )
    return ctx


def _settle(ctx, i: int = 0) -> None:
    # The mutation's then handler is a microtask that drains after this
    # eval; the separate rerender eval then sees its state updates.
    ctx.eval(f"__settlePut({i});")
    ctx.eval("MR.rerender();")


def test_a_save_puts_the_typed_source_and_toasts() -> None:
    # Characterization: Save sends the typed source (plus the fields the
    # form owns) and toasts on success.
    ctx = _ctx()
    try:
        ctx.eval('__type("src0 edited");')
        assert _js(ctx, "__canSave()") is True
        ctx.eval("__save();")
        p = _js(ctx, "PUTS")[0]
        assert p["payload"]["id"] == "ts1"
        assert p["payload"]["provider"] == "python"
        assert p["payload"]["config"]["source"] == "src0 edited"
        assert p["payload"]["config"]["source_version"] == 2
        _settle(ctx)
        assert _js(ctx, "TOASTS")[0]["title"] == "Toolset saved"
        assert len(_js(ctx, "PUTS")) == 1
    finally:
        ctx.close()


def test_keystrokes_typed_during_a_save_survive_the_resolve() -> None:
    # The keystrokes of the request window stay in the editor after the PUT
    # resolves.
    ctx = _ctx()
    try:
        ctx.eval('__type("src0 edited"); __save();')
        ctx.eval('__type("src0 edited, more");')
        _settle(ctx)
        assert _js(ctx, "__value()") == "src0 edited, more", (
            "keystrokes typed while the save was out were thrown away"
        )
    finally:
        ctx.close()


def test_the_saved_source_stays_on_screen_until_the_refetch_lands() -> None:
    # Until the invalidation refetch answers, the editor shows what was just
    # saved, not the stale stored source; the caught-up refetch ends the
    # dirty state, and a divergent write replaces it.
    ctx = _ctx()
    try:
        ctx.eval('__type("v1"); __save();')
        _settle(ctx)
        assert _js(ctx, "__value()") == "v1", (
            "the stale stored source shows before the refetch lands"
        )
        assert _js(ctx, "__canSave()") is True
        ctx.eval('__land("v1");')
        assert _js(ctx, "__value()") == "v1"
        assert _js(ctx, "__canSave()") is False, (
            "the caught-up refetch must end the dirty state"
        )
        ctx.eval('__land("agent v2");')
        assert _js(ctx, "__value()") == "agent v2"
    finally:
        ctx.close()


def test_the_save_button_follows_dirty() -> None:
    # Characterization: Save is disabled with no edits and enabled with some.
    ctx = _ctx()
    try:
        assert _js(ctx, "__canSave()") is False
        ctx.eval('__type("x");')
        assert _js(ctx, "__canSave()") is True
    finally:
        ctx.close()
