"""A drag-select that ends on a scrim does not close the surface over it (audit G1-19).

A browser click goes to the nearest common ancestor of the press and the release. A drag that starts inside a panel (selecting the typed query,
a line of a file) and ends on the scrim behind it therefore clicks the scrim although the press was inside. The shared Modal and the console
overlay (``nv-scrim``) tell that gesture from a plain click on the scrim with a ``scrimDownRef`` pointer-down guard (tickets 01a14f37, #803, #830):
the scrim closes only when the press AND the click both land on it. The audit found the same bug in three more places, fixed here the same way:

* the phone ``BottomSheet`` (``ui/components/shared/bottom-sheet.jsx``), whose scrim closed on ANY click that reached it;
* the graph builder's add-step palette (``ui/components/graph-builder/gb-palette.jsx``), which lost the typed query;
* the session's file lightbox (``NV_Lightbox`` in ``ui/components/console/nv-session-doc.jsx``).

The review of that fix (ticket 01a16b18) found four more scrims that closed on ANY click and gave them the same guard:

* the command palette (``NV_Palette``, ``ui/components/console/nv-palette.jsx``), which lost the typed query;
* the trace maximize overlay (``NV_TraceMaximize``) and the graph view overlay (inside ``NV_SessionDoc``), both in ``nv-session-doc.jsx``;
* the semantic-search provider form (``SSPCreateModal``, ``ui/components/semantic-search.jsx``), which lost the half-filled form.

Each real component runs in V8 on the mini React runtime (``tests/ui/_mini_react.py``); only the focus and Escape hooks, which are outside this
interaction, are stubbed, plus what a surface needs to mount at all (the console handle, the resources it reads), named next to its row.
A surface that closes through its own state rather than an ``onClose`` prop (the palette, the graph view) says how to tell: ``GONE``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHEET = UI / "components" / "shared" / "bottom-sheet.jsx"
PALETTE = UI / "components" / "graph-builder" / "gb-palette.jsx"
DOC = UI / "components" / "console" / "nv-session-doc.jsx"
CONSOLE_PALETTE = UI / "components" / "console" / "nv-palette.jsx"
SEMANTIC_SEARCH = UI / "components" / "semantic-search.jsx"
SHELL_STATUS = (UI / "foundation" / "shell-status.js").read_text(encoding="utf-8")
SHELL_TURNS = (UI / "foundation" / "shell-turns.js").read_text(encoding="utf-8")
SESSION_STORE = (UI / "foundation" / "session-store.js").read_text(encoding="utf-8")
SHELL_ATTENTION = (UI / "foundation" / "shell-attention.js").read_text(encoding="utf-8")
SHELL_VERBS = (UI / "foundation" / "shell-verbs.js").read_text(encoding="utf-8")
SESSION_ADAPTER = UI / "components" / "session-adapter.jsx"

_STUBS = r"""
window.primerApi = Object.assign(window.primerApi || {}, {
  useFocusTrap: function () {},
  focusablesOf: function () { return []; },
  useEscape: function () {},
});
var document = { body: { style: {} } };
function Icon() { return null; }
function GB_KindDot() { return null; }
var CLOSED = 0;
function onClose() { CLOSED += 1; }
"""

# The gesture: a pointer press on `pressTarget`, then the click the browser sends to the scrim (its target is the scrim when the release is on it).
# A surface closes either through its `onClose` prop (counted in CLOSED) or through its own state, in which case its mount names GONE: a function that
# is true once the surface has left the tree. closedCount() re-renders first, so a state change the click made is in the tree it reads.
_DRIVER = r"""
var SCRIM, PANEL, GONE = null;
function evt(target) {
  return { target: target, currentTarget: SCRIM, preventDefault: function () {}, stopPropagation: function () {} };
}
function pressThenClick(pressTarget) {
  if (SCRIM.props.onPointerDown) SCRIM.props.onPointerDown(evt(pressTarget));
  SCRIM.props.onClick(evt(SCRIM));
}
function closedCount() {
  MR.rerender();
  return CLOSED + (GONE && GONE() ? 1 : 0);
}
"""

# What the command palette needs to mount: the console handle it reads (the registry and every list empty), and the resources it reads (nothing served).
_PALETTE_STUBS = r"""
var CON = { paletteRef: { current: {} }, wid: "w1", registry: [], doc: null, frecency: null, workspaces: [] };
function NV_useConsole() { return CON; }
function SH_rankVerbs() { return []; }
function NV_identity() { return null; }
var SH_api = {};
window.addEventListener = function () {}; window.removeEventListener = function () {};
window.primerApi = Object.assign(window.primerApi || {}, {
  useResource: function () { return { data: null, error: null, loading: false, refetch: function () {} }; },
});
"""

# What the provider form needs to mount outside a frame (so it draws its own overlay): the router, mutation and capability hooks, /ssp/_types served empty,
# and the two kit components it draws (a button and a labelled field; their looks are outside this interaction).
_SSP_STUBS = r"""
function Btn(props) { return React.createElement("button", { onClick: props.onClick }, props.children); }
window.FormField = function (props) { return React.createElement("div", null, props.children); };
window.primerApi = Object.assign(window.primerApi || {}, {
  useRouter: function () { return { navigate: function () {}, params: {} }; },
  useMutation: function () { return { mutate: function () { return Promise.resolve(); }, loading: false }; },
  useResource: function () { return { data: {}, error: null, loading: false, refetch: function () {} }; },
  resourceState: function () { return "ready"; },
  useCapabilities: function () { return { data: { extras: {} } }; },
  extraInstalled: function () { return true; },
  capabilityHint: function (x) { return String(x); },
  apiFetch: function () { return Promise.resolve({}); },
});
"""

# What a graph session's document needs to mount: the console handle, the live-frame hooks (no tap events, an empty store), and the resources it reads (only the
# session row is served: a graph session, which is what draws the "Graph view" button). SD_GraphRunView, the canvas the overlay hosts, is a stand-in.
_DOC_STUBS = r"""
var CON = { wid: "w1", toast: function () {}, setDoc: function () {}, voiceRef: { current: null }, registry: SH_createVerbRegistry() };
function NV_useConsole() { return CON; }
function NV_identity() { return { color: "currentColor", d: "" }; }
function setInterval() { return 0; } function clearInterval() {}
function setTimeout() { return 0; } function clearTimeout() {}
var SESSION_ROW = { session_id: "s1", binding: { kind: "graph", graph_id: "g1" } };
var SH_api = { keys: new Proxy({}, { get: function (t, name) { return function (sid) { return String(name) + ":" + sid; }; } }) };
window.useWorkspaceTap = function () { return { events: [] }; };
window.useWorkspaceTapListener = function () {};
window.useSessionStore = function (wid, sid, channel) { return window.SS_getSnapshot(window.SS_getStore(wid, sid), channel); };
window.SD_GraphRunView = function () { return null; };
window.primerApi = Object.assign(window.primerApi || {}, {
  useViewport: function () { return { isMobile: false }; },
  useResource: function (key) {
    return { data: key === "session:s1" ? SESSION_ROW : null, error: null, loading: false, refetch: function () {} };
  },
});
"""

_MOUNTS = {
    "bottom sheet": (
        SHEET,
        "",
        'MR.mount(BottomSheet, { open: true, title: "Fact sheet", onClose: onClose });'
        ' SCRIM = MR.findProp("className", "sheet-overlay"); PANEL = MR.findProp("className", "sheet");',
    ),
    "add-step palette": (
        PALETTE,
        "",
        "MR.mount(GB_AddStepPalette, { draft: { nodes: [] }, afterNodeId: null, tools: [], catalogue: [], onClose: onClose,"
        " onCreate: function () {}, onAddBranch: function () {} });"
        ' SCRIM = MR.find("gb-palette-scrim"); PANEL = MR.find("gb-palette");',
    ),
    "file lightbox": (
        DOC,
        SHELL_STATUS,
        'MR.mount(NV_Lightbox, { title: "notes.md", content: "line one", onClose: onClose });'
        ' SCRIM = MR.find("nv-lightbox"); PANEL = MR.findProp("className", "nv-lightbox-body");',
    ),
    "command palette": (
        CONSOLE_PALETTE,
        _PALETTE_STUBS,
        "MR.mount(NV_Palette, {}); CON.paletteRef.current.open(); MR.rerender();"
        ' SCRIM = MR.find("nv-palette-scrim"); PANEL = MR.find("nv-palette");'
        ' GONE = function () { return !MR.find("nv-palette-scrim"); };',
    ),
    "graph view": (
        DOC,
        SHELL_STATUS + SHELL_TURNS + SESSION_STORE + SHELL_ATTENTION + SHELL_VERBS + transpile(SESSION_ADAPTER) + _DOC_STUBS,
        'MR.mount(NV_SessionDoc, { sid: "s1" }); MR.click("nv-graph-view");'
        ' SCRIM = MR.find("nv-graph-view-scrim"); PANEL = MR.findProp("className", "nv-graph-overlay");'
        ' GONE = function () { return !MR.find("nv-graph-view-scrim"); };',
    ),
    "trace maximize": (
        DOC,
        SHELL_STATUS + SHELL_TURNS,
        "MR.mount(NV_TraceMaximize, { flatRows: [], traceNodes: [], turnNo: 1, agentName: null, isGraph: false, onClose: onClose });"
        ' SCRIM = MR.find("nv-trace-maximize-scrim"); PANEL = MR.find("nv-trace-maximize");',
    ),
    "provider form": (
        SEMANTIC_SEARCH,
        _SSP_STUBS,
        "MR.mount(SSPCreateModal, { onClose: onClose, pushToast: function () {}, initialProvider: \"pgvector\" });"
        ' SCRIM = MR.find("ssp-modal-scrim"); PANEL = MR.findProp("className", "modal");',
    ),
}


@pytest.fixture(scope="module", params=sorted(_MOUNTS))
def surface(request):
    path, extra, mount = _MOUNTS[request.param]
    ctx = mini_react_context(transpile(path), "var window = globalThis;\n" + extra + _STUBS + _DRIVER)
    ctx.eval("function remount() { CLOSED = 0; " + mount + " }")
    try:
        yield ctx
    finally:
        ctx.close()


def test_the_scrim_and_the_panel_are_found(surface) -> None:
    surface.eval("remount();")
    assert surface.eval("!!SCRIM && !!PANEL")


def test_a_drag_select_that_starts_inside_and_ends_on_the_scrim_keeps_it_open(surface) -> None:
    surface.eval("remount(); pressThenClick(PANEL);")
    assert surface.eval("closedCount()") == 0


def test_a_plain_click_on_the_scrim_still_closes_it(surface) -> None:
    surface.eval("remount(); pressThenClick(SCRIM);")
    assert surface.eval("closedCount()") == 1
