"""After consolidation, window.GR_Canvas IS the G6 canvas: it lives in the
canonical graph-canvas.jsx, the SVG primitives are gone, and the spike-named
graph-g6-canvas.jsx no longer exists. Source-grep + transpile; the live-stack
gate confirms both surfaces still render via the single canvas."""
from __future__ import annotations
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
CANVAS = (UI / "components" / "graph-canvas.jsx").read_text(encoding="utf-8")
INDEX = (UI / "index.html").read_text(encoding="utf-8")


def test_gr_canvas_is_g6() -> None:
    assert "window.GR_Canvas" in CANVAS
    assert "function GR_Canvas" in CANVAS
    assert "window.G6" in CANVAS  # it is the G6 implementation
    assert "GR_NODE_SIZE" in CANVAS


def test_spike_g6_file_deleted() -> None:
    assert not (UI / "components" / "graph-g6-canvas.jsx").exists()
    assert "components/graph-g6-canvas.jsx" not in INDEX
    assert "components/graph-canvas.jsx" in INDEX


def test_no_svg_primitives_remain() -> None:
    # The SVG node/edge primitives are gone from every component file.
    for src in (UI / "components").glob("*.jsx"):
        t = src.read_text(encoding="utf-8")
        assert "function GR_NodeBox" not in t
        assert "function GR_EdgePath" not in t
        assert "function GR_SingleEdge" not in t


def test_no_g6canvas_name_leaks() -> None:
    # The spike component name is fully retired across ui/.
    for src in (UI / "components").glob("*.jsx"):
        assert "GR_G6Canvas" not in src.read_text(encoding="utf-8")


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
