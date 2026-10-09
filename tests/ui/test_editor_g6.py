"""The spike G6-editor POC, its toggle, and the SVG-drag machinery are gone, and the
bundle transpiles. (That the editor renders through the shared G6 canvas with move and
connect wired is pinned on the builder in test_graph_builder_parity.py; the live-stack
gate confirms the editor gestures actually work.)"""
from __future__ import annotations
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
GRAPHS = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")


def test_spike_editor_removed() -> None:
    assert "g6EditOn" not in GRAPHS
    assert "GR_G6Editor" not in GRAPHS
    assert "primer.g6Editor" not in GRAPHS
    # SVG-drag mechanics retired (G6 owns dragging now)
    assert "onNodeMouseDown" not in GRAPHS


def test_poc_file_deleted() -> None:
    assert not (UI / "components" / "graph-g6-editor.jsx").exists()
    assert "graph-g6-editor.jsx" not in (UI / "index.html").read_text(encoding="utf-8")


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
