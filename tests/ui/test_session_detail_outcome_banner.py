"""Terminal sessions get a decoded outcome banner; parked panels gain live
countdowns; panel-less parks get a generic waiting line. All from the
shared describeSessionState / SessionCountdown."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DETAIL = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_outcome_banner_uses_decoder() -> None:
    assert "describeSessionState" in DETAIL
    assert "data-testid=\"session-outcome\"" in DETAIL


def test_panels_show_countdown() -> None:
    assert "SessionCountdown" in DETAIL


def test_generic_waiting_line_for_panelless_park() -> None:
    assert "waitingOn" in DETAIL


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
