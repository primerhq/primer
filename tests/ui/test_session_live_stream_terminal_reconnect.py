"""SessionLiveStream must not live-tail a terminal session forever.

A terminal run has no live frames to tail — the REST /messages history is the
full transcript — so the live view (re-expressed as a single-session workspace
tap over EventSource in Task 6.2) must NOT open the tap for an ended / failed /
cancelled session. The tap effect is gated on a terminal ref so a terminal
session never connects, and a run that ends mid-stream stops tailing.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DETAIL = (ROOT / "ui" / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_terminal_ref_present() -> None:
    assert "terminalRef" in DETAIL


def test_tap_skipped_when_terminal() -> None:
    # The tap (live) effect must bail before opening the EventSource once the
    # session is terminal — the analogue of the old WS reconnect gate.
    assert "if (terminalRef.current)" in DETAIL
    # And the live tail is an EventSource tap, not a WebSocket.
    assert "EventSource" in DETAIL


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(ROOT / "ui")
    assert etag and body
