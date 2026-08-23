"""Static JSX checks — the read-only session usage meter.

The live session transcript (SessionLiveStream) is now PURE CONTENT (#10): its
header chrome, including the in-stream TokenMeter, was removed so session
controls live in exactly one place: the session document's own head. Since
the three-view flag day that head is nv-session-doc's usage bar
(NV_usageOf), read-only by construction - compaction is a disabled
overflow item until its endpoint lands, never a meter button.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
SESSION_DOC = UI / "components" / "console" / "nv-session-doc.jsx"
SESSION_JSX = UI / "components" / "session-detail.jsx"


def test_session_head_imports_token_meter() -> None:
    src = SESSION_DOC.read_text(encoding="utf-8")
    assert "NV_usageOf" in src and "nv-usage-bar" in src


def test_session_meter_is_read_only() -> None:
    """Read-only meter: the usage bar carries no compact affordance."""
    src = SESSION_DOC.read_text(encoding="utf-8")
    assert "onCompact" not in src


def test_embedded_stream_has_no_token_meter_chrome() -> None:
    """The embedded live stream is pure content — no TokenMeter in its body (#10)."""
    src = SESSION_JSX.read_text(encoding="utf-8")
    assert "window.TokenMeter" not in src
