"""The canvas lives in a shared module (graph-canvas.jsx, now the G6
renderer) so the run view and editor both reuse it. It must be registered
in the bundle before graphs.jsx and the bundle must still transpile."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CANVAS = UI / "components" / "graph-canvas.jsx"
GRAPHS = UI / "components" / "graphs.jsx"
INDEX = UI / "index.html"


def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_canvas_module_exists_and_exports() -> None:
    src = CANVAS.read_text(encoding="utf-8")
    assert "const GR_NODE_SIZE" in src
    assert "function GR_Canvas" in src
    # window-attached so the run view + editor (separate files) reference it.
    assert "window.GR_Canvas" in src
    assert "window.GR_NODE_SIZE" in src


def test_canvas_definitions_removed_from_graphs() -> None:
    src = GRAPHS.read_text(encoding="utf-8")
    # The definitions moved out; graphs.jsx no longer DEFINES them.
    assert "const GR_NODE_SIZE = {" not in src
    assert "const GR_Canvas = React.forwardRef" not in src


def test_canvas_registered_before_graphs_in_bundle() -> None:
    order = _order()
    assert "components/graph-canvas.jsx" in order
    assert order.index("components/graph-canvas.jsx") < order.index(
        "components/graphs.jsx"
    )


def test_bundle_transpiles_with_canvas_module() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/graph-canvas.jsx === */" in text
