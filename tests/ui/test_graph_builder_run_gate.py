"""The builder's Run respects the draft it starts from (UI-M11, ruling D10).

A run starts from the SAVED graph: ``GB_api.startRun`` posts the graph id, not the
draft. So with unsaved edits the drawer's Run used to start a run of the stale saved
graph while the operator believed their edits were in it; ruling D10 disables Run
while the draft is dirty, with a short hint to save first. And a double press of Run
used to start two runs (two ``POST /workspaces/{wid}/sessions``): the handler has a
``runBusy`` state for the button and a ref guard for the same-frame second click that
the state has not re-rendered yet.

``GB_Builder`` (``ui/components/graph-builder/graph-builder.jsx``) runs in V8 on the
shared builder harness (``tests/ui/_graph_builder_v8.py``), with a recording
``apiFetch`` so a test counts what Run sends.
"""

from __future__ import annotations

import json

from tests.ui._mini_react import mini_react_context
from tests.ui._graph_builder_v8 import DRIVER, builder_code

PRELUDE = r"""
window.requestAnimationFrame = function () { return 0; }; window.cancelAnimationFrame = function () {};
window.addEventListener = function () {}; window.removeEventListener = function () {};
var TOOLS = [{ id: "ts__echo", description: "echo", input_schema: { type: "object", properties: { msg: { type: "string" } }, required: ["msg"] } }];
var __seen = [];
var __navigated = [];
var __toasted = [];
var __api = {};
window.primerApi = {
  useResource: function () { return { data: { items: TOOLS }, loading: false, error: null, refetch: function () {} }; },
  useMutation: function () { return { mutate: function () {}, loading: false }; },
  usePagedList: function () { return { items: [], loading: false }; },
  Pager: function () { return null; },
  apiFetch: function (method, path, body) {
    __seen.push([method, path, body === undefined ? null : body]);
    var stub = __api[method + " " + path];
    if (!stub) return Promise.reject({ status: 500, detail: "no stub for " + method + " " + path });
    return Promise.resolve(stub);
  },
  useRouter: function () { return { navigate: function (path, opts) { __navigated.push([path, opts || null]); }, params: {} }; },
  useEscape: function () {},
  useFocusTrap: function () {},
  focusablesOf: function () { return []; },
};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, title: p.title, onClick: p.busy ? function () {} : p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", null, p.children); }
function Icon() { return null; }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.title, p.children, p.footer); }
window.Btn = Btn; window.Banner = Banner; window.Icon = Icon; window.Modal = Modal;
"""

# A draft that passes every validation tier (the saved graph Run would start from).
DRAFT = {
    "id": "g", "description": "d",
    "nodes": [{"kind": "begin", "id": "s"}, {"kind": "end", "id": "f"}],
    "edges": [{"kind": "static", "from_node": "s", "to_node": "f"}],
}

# The same graph with one extra saved-field edit: an import with max_iterations.
# GB_stripAll strips only coordinates, so the draft is dirty and still passes
# every validation tier (canRun stays true, nothing blocks it).
DIRTY_SPEC = {**DRAFT, "max_iterations": 5}

DRIVER_LOCAL = r"""
function __stub(method, path, response) { __api[method + " " + path] = response; }
function __mount(loaded) {
  MR.mount(GB_Builder, {
    graphId: "g", loaded: loaded,
    pushToast: function (t) { __toasted.push(t); },
    onSaved: function () {}, onRefresh: function () {},
  });
}
function __openDryRun() {
  (function () {
    var btn = null;
    (function w(n) {
      if (btn || n == null || typeof n !== "object") return;
      if (Array.isArray(n)) { n.forEach(w); return; }
      if (!n.__el) return;
      if (n.type === "button" && n.props && n.props.children === "Dry run") { btn = n; return; }
      if (typeof n.type === "function" && n.type !== React.Fragment) w(n.out); else w(n.children);
    })(MR.find("gb-builder"));
    btn.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} });
  })();
  MR.rerender();
}
// The drawer's Run button is a raw <button> with no testid; it is the only
// button in the drawer other than the header's "Re-check" (gb-dryrun-run).
function __runButton() {
  var out = [];
  (function w(n) {
    if (n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(w); return; }
    if (!n.__el) return;
    if (n.type === "button" && n.props["data-testid"] !== "gb-dryrun-run") { out.push(n); return; }
    if (typeof n.type === "function" && n.type !== React.Fragment) w(n.out); else w(n.children);
  })(MR.find("gb-dryrun"));
  if (!out.length) throw new Error("no run button in the dry-run drawer");
  return out[out.length - 1];
}
function __pressRunTwice() {
  var b = __runButton();
  var e = { preventDefault: function () {}, stopPropagation: function () {} };
  b.props.onClick(e);
  b.props.onClick(e);
  MR.rerender();
}
function __sessionPosts() {
  return __seen.filter(function (r) { return r[0] === "POST" && /\/sessions$/.test(r[1]); }).length;
}
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    ctx = mini_react_context(builder_code(), PRELUDE)
    ctx.eval(DRIVER + "\n" + DRIVER_LOCAL)
    return ctx


def _stub_run(ctx) -> None:
    ctx.eval("__stub('POST', '/graphs/g/dry_run', {ok: true, nodes: [], blockers: [], local: false, shape: {longest_path: 2, parallel_groups: 0, loops: 0, human_pauses: 0}})")
    ctx.eval("__stub('GET', '/workspaces?limit=200', {kind: 'offset', offset: 0, length: 1, items: [{id: 'ws-def', is_default: true}], total: 1})")
    ctx.eval("__stub('POST', '/workspaces/ws-def/sessions', {id: 'sess-1', workspace_id: 'ws-def'})")


def test_a_saved_draft_offers_run_with_no_blockers() -> None:
    # Characterization: the gate today - a clean draft whose graph passes every
    # tier keeps the Run button enabled under its own words.
    ctx = _ctx()
    try:
        _stub_run(ctx)
        ctx.eval(f"__mount({json.dumps(DRAFT)}); __openDryRun()")
        btn = _js(ctx, "(function () { var b = __runButton(); return { disabled: b.props.disabled, text: b.props.children }; })()")
        assert btn["disabled"] is False
        assert btn["text"] == "Run this graph"
    finally:
        ctx.close()


def test_a_dirty_draft_disables_run_with_a_save_first_hint() -> None:
    # D10: with unsaved edits the run would start from the stale saved graph,
    # so Run is disabled and says to save first (label and tooltip).
    ctx = _ctx()
    try:
        _stub_run(ctx)
        ctx.eval(f"__mount({json.dumps(DRAFT)}); load({json.dumps(DIRTY_SPEC)}); __openDryRun()")
        btn = _js(ctx, "(function () { var b = __runButton(); return { disabled: b.props.disabled, text: b.props.children, title: b.props.title || null }; })()")
        assert btn["disabled"] is True, "a dirty draft must not offer a run of the saved graph"
        assert "save" in btn["text"].lower(), f"the label is the short hint: {btn['text']!r}"
        assert btn["title"] and "save" in btn["title"].lower()
    finally:
        ctx.close()


def test_two_presses_of_run_start_one_session() -> None:
    # The second press lands in the same frame, before the first request has
    # answered: a ref guard must hold it back, not the not-yet-re-rendered state.
    ctx = _ctx()
    try:
        _stub_run(ctx)
        ctx.eval(f"__mount({json.dumps(DRAFT)}); __openDryRun(); __pressRunTwice()")
        assert _js(ctx, "__sessionPosts()") == 1
        assert [t for t in _js(ctx, "__toasted") if t.get("title") == "Run started"] and len([t for t in _js(ctx, "__toasted") if t.get("title") == "Run started"]) == 1
    finally:
        ctx.close()
