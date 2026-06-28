"""The session run-view renders through the unified G6 canvas (dagre layout);
the spike SVG/G6 toggle and the autoLayout pre-pass are gone. Source-grep +
transpile; the live-stack gate confirms status colors + icons actually draw."""
from __future__ import annotations
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
SD = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_run_view_uses_g6_dagre() -> None:
    assert "GR_G6Canvas" in SD
    assert 'layout="dagre"' in SD


def test_spike_toggle_removed() -> None:
    assert "g6On" not in SD
    assert "SD_G6Canvas" not in SD
    assert "primer.g6RunView" not in SD


def test_no_autolayout_prepass() -> None:
    # G6's dagre owns run-view layout now.
    assert "autoLayout" not in SD


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
