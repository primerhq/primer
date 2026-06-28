"""The unified G6 canvas renders all node kinds (icons) + all edge kinds
(static/conditional/implicit fan-out) + the status overlay, driven by a
layout prop. Source-grep + bundle-transpile; the live-stack gate (run
manually) confirms G6 actually draws them."""
from __future__ import annotations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = (UI / "components" / "graph-g6-canvas.jsx").read_text(encoding="utf-8")


def test_exports_unified_canvas() -> None:
    assert "function GR_G6Canvas" in SRC
    assert "window.GR_G6Canvas" in SRC
    assert "GR_NODE_SIZE" in SRC


def test_layout_prop_preset_and_dagre() -> None:
    assert '"preset"' in SRC or "'preset'" in SRC
    assert "dagre" in SRC


def test_renders_all_node_kinds() -> None:
    for kind in ("begin", "end", "agent", "tool_call", "fan_out", "fan_in", "graph"):
        assert kind in SRC
    assert "_g6IconUri" in SRC


def test_renders_all_edge_kinds() -> None:
    assert "conditional" in SRC
    assert "router" in SRC or "default_to" in SRC
    assert "specs" in SRC and "target_node_id" in SRC


def test_status_overlay_present() -> None:
    for s in ("pending", "running", "waiting", "ended", "failed"):
        assert s in SRC


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
