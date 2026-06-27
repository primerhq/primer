"""Graph-bound sessions get a two-pane run view: GR_Canvas (reused) with
a per-node status overlay on the left, an inspector shell on the right.
Gated on isGraph; agent sessions keep the plain live stream."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DETAIL = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_run_view_component_present() -> None:
    assert "function SD_GraphRunView" in DETAIL
    assert "window.SD_GraphRunView" in DETAIL


def test_run_view_reuses_shared_canvas() -> None:
    # Reuse, not duplicate: the run view renders the shared GR_Canvas and
    # delegates per-node status tinting to it via the statusTint prop
    # (rings live inside the canvas scroll, not a page-overflowing overlay).
    assert "GR_Canvas" in DETAIL
    assert "statusTint" in DETAIL


def test_run_view_polls_node_states() -> None:
    assert "/node_states" in DETAIL
    assert "runs/" in DETAIL


def test_run_view_status_tint_table() -> None:
    src = DETAIL
    assert "SD_RUN_STATE_TINT" in src
    for status in ("pending", "running", "waiting", "ended", "failed"):
        assert status in src


def test_run_view_gated_on_isgraph() -> None:
    # The run view replaces liveStreamPanel only for graph sessions.
    assert "isGraph" in DETAIL
    assert "SD_GraphRunView" in DETAIL


def test_bundle_transpiles_with_run_view() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "SD_GraphRunView" in body.decode("utf-8")
