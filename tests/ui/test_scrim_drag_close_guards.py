"""A drag-select that ends on a scrim does not close the surface over it (audit G1-19).

A browser click goes to the nearest common ancestor of the press and the release. A drag that starts inside a panel (selecting the typed query,
a line of a file) and ends on the scrim behind it therefore clicks the scrim although the press was inside. The shared Modal and the console
overlay (``nv-scrim``) tell that gesture from a plain click on the scrim with a ``scrimDownRef`` pointer-down guard (tickets 01a14f37, #803, #830):
the scrim closes only when the press AND the click both land on it. The audit found the same bug in three more places, fixed here the same way:

* the phone ``BottomSheet`` (``ui/components/shared/bottom-sheet.jsx``), whose scrim closed on ANY click that reached it;
* the graph builder's add-step palette (``ui/components/graph-builder/gb-palette.jsx``), which lost the typed query;
* the session's file lightbox (``NV_Lightbox`` in ``ui/components/console/nv-session-doc.jsx``).

Each real component runs in V8 on the mini React runtime (``tests/ui/_mini_react.py``); only the focus and Escape hooks, which are outside this
interaction, are stubbed.
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
SHELL_STATUS = (UI / "foundation" / "shell-status.js").read_text(encoding="utf-8")

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
_DRIVER = r"""
var SCRIM, PANEL;
function evt(target) {
  return { target: target, currentTarget: SCRIM, preventDefault: function () {}, stopPropagation: function () {} };
}
function pressThenClick(pressTarget) {
  if (SCRIM.props.onPointerDown) SCRIM.props.onPointerDown(evt(pressTarget));
  SCRIM.props.onClick(evt(SCRIM));
}
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
    assert surface.eval("CLOSED") == 0


def test_a_plain_click_on_the_scrim_still_closes_it(surface) -> None:
    surface.eval("remount(); pressThenClick(SCRIM);")
    assert surface.eval("CLOSED") == 1
