"""The live panels reused by the Studio (SleepPanel etc.) surface parked
countdowns via the shared SessionCountdown. The decoded outcome banner +
panel-less waiting line lived in the old full-page SessionDetail component,
which was removed (the Studio now subsumes the session view), so those
assertions are gone with it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DETAIL = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_panels_show_countdown() -> None:
    assert "SessionCountdown" in DETAIL


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
