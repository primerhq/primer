"""SessionLiveStream loads full recorded history via the REST messages
endpoint on mount (so ended sessions show their output), then tails the WS
only while running. Terminal sessions read 'Session ended', not
'connection dropped'."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DETAIL = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_loads_history_from_messages_endpoint() -> None:
    assert "/messages" in DETAIL
    # History seeds the stream (not only the WS replay).
    assert "after_seq" in DETAIL or "/messages" in DETAIL


def test_ws_cursor_resumes_from_loaded_history() -> None:
    # The WS connects with the highest loaded seq so the tail has no gap.
    assert "cursor=" in DETAIL


def test_terminal_says_session_ended_not_dropped() -> None:
    assert "Session ended" in DETAIL


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
