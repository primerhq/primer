"""SessionLiveStream must not reconnect a terminal session's WS forever.

A terminal run has no live frames to tail: the server accepts the socket,
replays (often nothing), then closes — and because onopen resets the
backoff, an unguarded reconnect loops at ~1s indefinitely. The reconnect
is gated on a terminal ref so an ended/failed/cancelled session opens the
socket at most once (for history replay) and then stops.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DETAIL = (ROOT / "ui" / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_terminal_ref_present() -> None:
    assert "terminalRef" in DETAIL


def test_reconnect_gated_on_terminal() -> None:
    # The WS reconnect branch must be skipped once the session is terminal.
    assert "!terminalRef.current" in DETAIL


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(ROOT / "ui")
    assert etag and body
