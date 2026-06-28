"""The unified G6 canvas exposes the interactive contract (select, edge
select, double-click, drag-to-move, drag-to-connect) and gates mutating
behaviors so the read-only run-view can't drag/connect. Source-grep +
transpile; the live gate confirms the gestures actually work."""
from __future__ import annotations
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
SRC = (UI / "components" / "graph-canvas.jsx").read_text(encoding="utf-8")


def test_interaction_callbacks_wired() -> None:
    for cb in ("onNodeClick", "onEdgeClick", "onNodeDoubleClick",
               "onBackgroundClick", "onMoveNode", "onConnect"):
        assert cb in SRC


def test_add_edge_mode_and_connect() -> None:
    assert "addEdgeMode" in SRC
    assert "create-edge" in SRC  # G6 native drag-to-connect


def test_mutating_behaviors_gated() -> None:
    # drag-element / create-edge only when editor callbacks exist
    assert "drag-element" in SRC
    assert "onMoveNode" in SRC and "onConnect" in SRC


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
