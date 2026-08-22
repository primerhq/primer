"""Static JSX checks — the read-only session TokenMeter.

The live session transcript (SessionLiveStream) is now PURE CONTENT (#10): its
header chrome, including the in-stream TokenMeter, was removed so session
controls live in exactly one place: the session document's own head. The
read-only meter therefore lives in the shell session head
(sh-session-doc.jsx :: SH_TokenMeter), which reuses the shared
window.TokenMeter with onCompact={null}.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
SESSION_DOC = UI / "components" / "shell" / "sh-session-doc.jsx"
SESSION_JSX = UI / "components" / "session-detail.jsx"


def test_session_head_imports_token_meter() -> None:
    src = SESSION_DOC.read_text(encoding="utf-8")
    assert "TokenMeter" in src


def test_session_meter_is_read_only() -> None:
    """Read-only meter: onCompact must be null (no compact button on sessions)."""
    src = SESSION_DOC.read_text(encoding="utf-8")
    assert "onCompact={null}" in src or "onCompact=null" in src


def test_embedded_stream_has_no_token_meter_chrome() -> None:
    """The embedded live stream is pure content — no TokenMeter in its body (#10)."""
    src = SESSION_JSX.read_text(encoding="utf-8")
    assert "window.TokenMeter" not in src
