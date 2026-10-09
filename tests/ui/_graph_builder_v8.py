"""The V8 stand-in for the real graph builder, shared by the tests that mount it (``GB_Builder``, ``GB_Inspector``, ``GB_BranchBuilder`` ... on the strict mini React).

``builder_code()`` is every builder file as the server's bundler would emit it; ``PRELUDE`` stubs what the console around the builder provides (``window.primerApi``, ``Btn``, ``Modal`` ...);
``DRIVER`` is the JS helpers a test drives it with (``mountBuilder``, ``selectNode``, ``inspector``, ``probeDraft`` ...). Moved out of ``test_graph_builder_import_renders.py`` so that a test of another
part of the builder does not import private names from that module.
"""

from __future__ import annotations

import functools
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
FILES = [
    "components/shared/entity-picker.jsx", "components/shared/form-field.jsx", "components/graph-canvas.jsx",
    "components/graph-builder/gb-model.jsx", "components/graph-builder/gb-api.jsx", "components/graph-builder/gb-refs.jsx",
    "components/graph-builder/gb-validate.jsx", "components/graph-builder/gb-canvas.jsx", "components/graph-builder/gb-outline.jsx",
    "components/graph-builder/gb-palette.jsx", "components/graph-builder/gb-schema.jsx", "components/graph-builder/gb-ref-editor.jsx",
    "components/graph-builder/gb-branches.jsx", "components/graph-builder/gb-inspector.jsx", "components/graph-builder/gb-readiness.jsx",
    "components/graph-builder/gb-dryrun.jsx", "components/graph-builder/gb-starters.jsx", "components/graph-builder/graph-builder.jsx",
    "components/graphs.jsx",
]

PRELUDE = r"""
window.requestAnimationFrame = function () { return 0; }; window.cancelAnimationFrame = function () {};
window.addEventListener = function () {}; window.removeEventListener = function () {};
var TOOLS = [{ id: "ts__echo", description: "echo", input_schema: { type: "object", properties: { msg: { type: "string" } }, required: ["msg"] } }];
var OPTS = { knownToolIds: ["ts__echo"] };
window.primerApi = {
  useResource: function () { return { data: { items: TOOLS } }; },
  useMutation: function () { return { mutate: function () {}, loading: false }; },
  usePagedList: function () { return { items: [], loading: false }; },
  Pager: function () { return null; },
  apiFetch: function () { return Promise.resolve({}); },
  useRouter: function () { return { navigate: function () {} }; },
};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", null, p.children); }
function Icon() { return null; }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.title, p.children, p.footer); }
window.Btn = Btn; window.Banner = Banner; window.Icon = Icon; window.Modal = Modal;
"""

DRIVER = r"""
function errOf(fn) { try { fn(); return null; } catch (e) { return (typeof e === "string") ? ("STRING:" + e) : String((e && e.message) || e).slice(0, 160); } }
function findType(node, T) {
  var found = null;
  (function w(n) {
    if (found || n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(w); return; }
    if (!n.__el) return;
    if (n.type === T) { found = n; return; }
    if (typeof n.type === "function" && n.type !== React.Fragment) w(n.out); else w(n.children);
  })(node);
  return found;
}
var NOOP = function () {};
function inspectorProps(draft, node, edgeIdx) {
  return { draft: draft, node: node, edgeIdx: edgeIdx, dispatch: NOOP, tools: TOOLS, readOnly: false, problems: null, onSelectNode: NOOP, onJsonError: NOOP };
}
function Blank() { return null; }
// Everything a draft meets once it is imported, each in a fresh mount; the import's own refusal first.
function probeDraft(current, spec) {
  MR.mount(Blank, {});
  var out = { refused: GB_importProblem(spec, OPTS) };
  if (out.refused) return out;
  var draft = GB_reducer(current, { type: "IMPORT_SPEC", spec: spec });
  var crashes = {};
  var e0 = errOf(function () { MR.mount(GB_Builder, { graphId: "g", loaded: draft, pushToast: NOOP }); });
  if (e0) crashes.builder = e0;
  var e1 = errOf(function () { (draft.nodes || []).forEach(function (n) { _g6Label(n, null); }); });
  if (e1) crashes.canvasLabel = e1;
  (draft.nodes || []).forEach(function (n) {
    var a = errOf(function () { MR.mount(GB_Inspector, inspectorProps(draft, n, null)); });
    if (a) crashes["inspector " + n.id] = a;
    var b = errOf(function () { MR.mount(GB_RefPicker, { draft: draft, nodeId: n.id, onPick: NOOP, onClose: NOOP }); });
    if (b) crashes["picker " + n.id] = b;
  });
  (draft.edges || []).forEach(function (e, i) {
    var c = errOf(function () { MR.mount(GB_Inspector, inspectorProps(draft, null, i)); });
    if (c) crashes["edge " + i] = c;
  });
  out.crashes = crashes;
  return out;
}
function probeAll(current, specs) {
  var bad = [];
  specs.forEach(function (s) {
    var r = probeDraft(current, s.spec);
    if (!r.refused && Object.keys(r.crashes).length) bad.push({ where: s.where, crashes: r.crashes });
  });
  return bad;
}
// The real flow: a builder on `current`, optional selection, open the JSON modal and Load `spec` through its onApply, render.
function openModal() {
  MR.click("gb-json-tab");
  return findType(MR.find("gb-builder"), GR_ImportSpecModal);
}
function selectNode(id) {
  var row = MR.findAll("gb-outline-row").filter(function (el) { return el.props["data-node-id"] === id; })[0];
  row.props.onClick(); MR.rerender();
}
function inspector() { return findType(MR.find("gb-builder") || MR.find("gb-render-error"), GB_Inspector); }
function mountBuilder(loaded) { MR.mount(GB_Builder, { graphId: "g", loaded: loaded, pushToast: NOOP }); }
function load(spec) { var m = openModal(); m.props.onApply(spec); MR.rerender(); }
// A step the test can make fail: the inspector of a draft whose description is POISON throws, and so does the inspector of the step named in __POISON_NODE (a throw at click time).
var __realInspector = GB_Inspector;
var __POISON_NODE = null;
GB_Inspector = function (p) {
  if (p.draft && p.draft.description === "POISON") throw new Error("poison in the inspector");
  if (__POISON_NODE && p.node && p.node.id === __POISON_NODE) throw new Error("poison in the step " + __POISON_NODE);
  return __realInspector(p);
};
"""


@functools.lru_cache(maxsize=1)
def builder_code() -> str:
    from primer.api._jsx_bundle import JSXBundler

    bundler = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    try:
        return "\n".join(bundler._transform((UI / rel).read_text(encoding="utf-8"), rel) for rel in FILES)
    finally:
        bundler._ctx.close()
