"""Structural checks for the compact, content-aware WorkspaceTap event rows.

Part B of the studio-activity rework: each tap event row shows a MINIMAL
timestamp + class chip + a class-tuned payload PREVIEW with VARIABLE height
(not uniform boxes). These are static-source checks (no React render), matching
the approach in test_studio_activity.py / test_studio_live_consolidation.py.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TAP = UI / "components" / "workspace-tap.jsx"


def _tap_src() -> str:
    return TAP.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Content-aware preview builder
# ---------------------------------------------------------------------------

def test_content_aware_preview_helper_present() -> None:
    src = _tap_src()
    assert "function WTP_eventContent(" in src
    # The old one-line summary is replaced.
    assert "function WTP_payloadSummary(" not in src


def test_preview_handles_each_class() -> None:
    src = _tap_src()
    body = src[src.index("function WTP_eventContent("):]
    # Cut the helper body at the next top-level function.
    end = body.index("\nfunction ", 1)
    body = body[:end]
    for cls in (
        "assistant_token",
        "user_input",
        "tool_call",
        "tool_result",
        "graph_transition",
        "done",
        "error",
    ):
        assert f'"{cls}"' in body, f"WTP_eventContent does not handle {cls}"


def test_preview_shows_type_specific_fields() -> None:
    src = _tap_src()
    # assistant / user text
    assert "p.text" in src
    # tool_call args / tool name
    assert "p.arguments" in src
    assert "p.tool_name" in src
    # tool_result output / error
    assert "p.output" in src
    # graph transition
    assert "p.node_id" in src and "p.phase" in src
    # done outcome + usage
    assert "p.stop_reason" in src
    assert "p.usage" in src
    # error outcome
    assert "p.message" in src


def test_preview_truncates_long_payloads() -> None:
    src = _tap_src()
    # A clamp helper bounds long payloads.
    assert "function WTP_clip(" in src


# ---------------------------------------------------------------------------
# Variable-height rows (not uniform boxes)
# ---------------------------------------------------------------------------

def test_rows_have_variable_height() -> None:
    src = _tap_src()
    # Per-class line clamp drives variable height.
    assert "clampLines" in src
    assert "WebkitLineClamp" in src
    # A distinct number of clamp lines is chosen per class.
    assert "clampLines: 3" in src
    assert "clampLines: 1" in src


def test_minimal_timestamp() -> None:
    src = _tap_src()
    # HH:MM:SS (minimal) rather than the old HH:MM:SS.mmm.
    assert "slice(11, 19)" in src


# ---------------------------------------------------------------------------
# Preserved contract: testids, class chip, filter chips, detail JSON
# ---------------------------------------------------------------------------

def test_preserved_testids_and_chip() -> None:
    src = _tap_src()
    for testid in (
        "workspace-tap-root",
        "tap-filter-bar",
        "tap-event-list",
        "activity-event",
        "tap-event-row",
        "activity-event-detail",
        "tap-event-preview",
    ):
        assert f'data-testid="{testid}"' in src, f"Missing testid: {testid}"
    # Class chip stays a `.pill` (e2e asserts >=1 pill per row).
    assert 'className="pill"' in src
    # Full-event detail JSON is still available on expand.
    assert "WTP_detailJson" in src


def test_filter_chips_preserved() -> None:
    src = _tap_src()
    assert "WTP_ALL_CLASSES" in src
    assert '"tap-filter-" + cls' in src
    # Client-side filter over the shared buffer still applies.
    assert "selectedClasses" in src


# ---------------------------------------------------------------------------
# Full bundle still transpiles
# ---------------------------------------------------------------------------

def test_bundle_transpiles_with_content_rows() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/workspace-tap.jsx === */" in text
