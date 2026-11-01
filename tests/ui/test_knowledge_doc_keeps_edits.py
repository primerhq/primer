"""A knowledge document's unsaved edits survive a re-read (UI-M3).

``KN_DocumentPane`` (``ui/components/knowledge.jsx``) re-seeded its ``draft``
and ``title`` from ``doc.data`` on every change of the read. After a save the
cached read still holds the pre-save body, so the wake refetch (tab regained)
or an agent's write landed a new read and the effect wiped whatever the
operator had typed in the meantime. Now the pane keeps the last seeded body in
a ref and re-seeds only when the path changed or the editor still holds exactly
what was seeded; a successful save also refetches the doc key so the cache
holds the saved body and the next wake refetch lands nothing new.

MiniRacer on the real file with a stubbed ``window.primerApi``: the
``useResource`` stub hands out a stable data identity the test swaps to model
a landed read, and ``apiFetch`` records the PATCH.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

KNOWLEDGE = Path(__file__).resolve().parents[2] / "ui" / "components" / "knowledge.jsx"

PRELUDE = r"""
globalThis.Btn = function (props) {
  return React.createElement("button", {
    "data-testid": props["data-testid"], onClick: props.onClick, disabled: props.disabled
  });
};
globalThis.Icon = function () { return null; };
globalThis.Banner = function (props) { return React.createElement("div", { "data-testid": "kn-banner" }, props.children); };
globalThis.window.confirmDialog = function () { return Promise.resolve(false); };
window.location = { origin: "http://x", search: "", hash: "", pathname: "/console/" };
var DOC = { data: { body: "b0", document: { title: "T0" }, children: [] } };
var SAVES = [];
var REFETCHES = 0;
var TOASTS = [];
window.primerApi = {
  apiFetch: function (method, url, payload) {
    if (method === "PATCH") {
      SAVES.push({ body: payload.body, title: payload.title });
      return Promise.resolve({});
    }
    return Promise.resolve(DOC.data);
  },
  useResource: function () {
    return { data: DOC.data, error: null, refetch: function () { REFETCHES++; } };
  }
};
"""

DRIVER = r"""
var __PROPS = {
  collection: { id: "c1" }, path: "docs/a", readOnly: false, ro: false,
  pushToast: function (t) { TOASTS.push(t); }, onChanged: function () {}, onSelect: function () {}
};
function __type(v) { MR.find("kn-doc-body").props.onChange({ target: { value: v } }); MR.rerender(); }
function __title(v) { MR.find("kn-doc-title").props.onChange({ target: { value: v } }); MR.rerender(); }
function __value() { return MR.find("kn-doc-body").props.value; }
function __titleValue() { return MR.find("kn-doc-title").props.value; }
function __clickSave() { MR.click("kn-doc-save"); }
function __saveLands() {
  // The PATCH's promise resolves between evals; the rerender then sees
  // busy back to false and the save's side effects.
  MR.rerender();
}
// Model a re-read landing (save refetch, agent write, tab wake): a new data
// identity with the given body and title.
function __landRead(body, title) {
  DOC.data = { body: body, document: { title: title || "T0" }, children: [] };
  MR.rerender();
}
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    ctx = mini_react_context(transpile(KNOWLEDGE), PRELUDE)
    ctx.eval(DRIVER)
    ctx.eval("MR.mount(KN_DocumentPane, __PROPS);")
    return ctx


def test_a_save_patches_the_draft_and_toasts() -> None:
    # Characterization: Save sends the typed body and title and toasts.
    ctx = _ctx()
    try:
        ctx.eval('__type("v1"); __title("T1"); __clickSave(); __saveLands();')
        assert _js(ctx, "SAVES") == [{"body": "v1", "title": "T1"}]
        assert len(_js(ctx, "TOASTS")) == 1
        assert _js(ctx, "TOASTS")[0]["title"] == "Document saved"
    finally:
        ctx.close()


def test_a_save_refetches_the_doc_key() -> None:
    # The cache still holds the pre-save body after a save; without a
    # refetch the next wake read lands it and the pane re-seeds over the
    # operator's next edit.
    ctx = _ctx()
    try:
        ctx.eval('__type("v1"); __clickSave(); __saveLands();')
        assert _js(ctx, "REFETCHES") >= 1, "the doc key was not refetched after a successful save"
    finally:
        ctx.close()


def test_a_wake_refetch_after_a_save_keeps_the_newer_edit() -> None:
    # Type, save, type more, then the saved body lands (the wake refetch's
    # answer): the editor keeps the newer edit, not the saved one.
    ctx = _ctx()
    try:
        ctx.eval('__type("v1"); __title("T1"); __clickSave(); __saveLands();')
        ctx.eval('__type("v2");')
        ctx.eval('__landRead("v1", "T1");')
        assert _js(ctx, "__value()") == "v2", "the wake re-read wiped the edit typed after the save"
        assert _js(ctx, "__titleValue()") == "T1"
    finally:
        ctx.close()


def test_an_agents_write_keeps_the_operators_unsaved_edit() -> None:
    # No save involved: while the operator holds an unsaved edit, a changed
    # read (an agent's write) must not re-seed over it.
    ctx = _ctx()
    try:
        ctx.eval('__type("v1");')
        ctx.eval('__landRead("agent", "T0");')
        assert _js(ctx, "__value()") == "v1", "the re-read wiped the operator's unsaved edit"
    finally:
        ctx.close()


def test_a_re_read_with_no_unsaved_edit_reseeds_the_editor() -> None:
    # Characterization: the editor still follows the server while it holds
    # nothing the operator typed.
    ctx = _ctx()
    try:
        assert _js(ctx, "__value()") == "b0"
        ctx.eval('__landRead("agent", "TA");')
        assert _js(ctx, "__value()") == "agent"
        assert _js(ctx, "__titleValue()") == "TA"
    finally:
        ctx.close()


def test_switching_paths_reseeds_the_editor() -> None:
    # Characterization: a path change always takes the new read's body, even
    # with an unsaved edit on the old path (the edit is abandoned with it).
    ctx = _ctx()
    try:
        ctx.eval('__type("v1");')
        ctx.eval('__landRead("bB", "TB");')
        ctx.eval('MR.rerender({ collection: { id: "c1" }, path: "docs/b", readOnly: false, ro: false, pushToast: function (t) { TOASTS.push(t); }, onChanged: function () {}, onSelect: function () {} });')
        assert _js(ctx, "__value()") == "bB"
    finally:
        ctx.close()
