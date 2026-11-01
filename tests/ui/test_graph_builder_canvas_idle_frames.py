"""An idle graph canvas must not set its measurement state on every frame (UI-M13).

``GB_Canvas`` measures the rendered node cards with a ``requestAnimationFrame``
loop so the overlays track pan and zoom. The measurement built a fresh object
per frame and handed it to ``setBoxes`` unconditionally, so an idle canvas
called setState at the display's refresh rate and re-rendered the canvas (and
G6's host) for no change. It now compares the measurement with the previous
one and only sets state on a real change.

Ported from the 2026-10-31 UI review probe: a fake G6 that answers the same
position for every frame, a counting ``useState``, and 10 idle frames must
leave at most one state change (the first measurement, when ``boxes`` is
still ``null``).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

UI = Path(__file__).resolve().parents[2] / "ui"

PRELUDE = r"""
var frames = [];
window.requestAnimationFrame = function (fn) { frames.push(fn); return frames.length; };
window.cancelAnimationFrame = function () {};
var FAKE_G = { getElementPosition: function (id) { return [100, 50]; }, getZoom: function () { return 1; } };
function GR_Canvas(p) { React.useEffect(function () { p.onGraphReady(FAKE_G); }, []); return React.createElement("div", null); }
window.GR_Canvas = GR_Canvas;
var GB_supersteps = function () { return []; };
var changes = 0;
var _us = React.useState;
React.useState = function (init) { var pair = _us(init); var set = pair[1]; var cur = pair[0]; return [cur, function (v) { if (!Object.is(v, cur)) changes += 1; set(v); }]; };
"""


def test_idle_frames_do_not_re_set_the_boxes() -> None:
    ctx = mini_react_context(transpile(UI / "components/graph-builder/gb-canvas.jsx"), PRELUDE)
    try:
        ctx.eval(
            "var DRAFT = { nodes: [{ id: 'a', kind: 'agent' }, { id: 'b', kind: 'end' }], edges: [] };"
            " MR.mount(window.GB_Canvas, { draft: DRAFT, showBands: false });"
        )
        ctx.eval("changes = 0;")
        for _ in range(10):
            ctx.eval("var f = frames.shift(); if (f) { f(); MR.rerender(); }")
        changes = json.loads(ctx.eval("JSON.stringify(changes)"))
        assert changes <= 1, f"an idle canvas set its measurement state {changes} times over 10 frames (only the first measurement may)"
    finally:
        ctx.close()
