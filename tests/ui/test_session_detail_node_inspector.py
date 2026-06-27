"""The node inspector renders kind-aware content using the SHARED frame
renderer (_SLS_Frame) and the existing per-node turn-log endpoint, with
the session live stream as the no-selection empty state."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DETAIL = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_inspector_reuses_shared_frame_renderer() -> None:
    # Reuse, not duplicate: the inspector renders node output through the
    # extracted _SLS_Frame rather than a second renderer.
    assert "_SLS_Frame" in DETAIL
    assert "_SLS_coalesceMessages" in DETAIL


def test_inspector_uses_per_node_turn_log_endpoint() -> None:
    assert "/turn_log" in DETAIL
    assert "/nodes/" in DETAIL


def test_inspector_empty_state_is_session_stream() -> None:
    # No node selected -> session-level live stream (the run's result).
    assert "SessionLiveStream" in DETAIL


def test_inspector_handles_node_kinds() -> None:
    # Header surfaces the node kind; the kind drives content selection.
    src = DETAIL
    assert "node.kind" in src
    for kind in ("agent", "begin", "end", "tool_call", "fan_out", "fan_in"):
        assert kind in src


def test_inspector_no_activity_neutral_state() -> None:
    # A node that never ran shows a neutral state, not an error (spec §9).
    assert "no activity yet" in DETAIL


def test_bundle_transpiles_with_inspector() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "SD_NodeInspector" in body.decode("utf-8")
