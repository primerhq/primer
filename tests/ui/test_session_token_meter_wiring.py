"""Static JSX checks — the read-only session TokenMeter.

The live session transcript (SessionLiveStream) is now PURE CONTENT (#10): its
header chrome — including the in-stream TokenMeter — was removed so session
controls live in exactly one place (the Studio panel header). The read-only
meter therefore lives in the Studio agent-panel header
(studio-center.jsx :: ST_TokenMeterInline), which reuses the shared
window.TokenMeter with onCompact={null}.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
CENTER_JSX = UI / "components" / "studio-center.jsx"
SESSION_JSX = UI / "components" / "session-detail.jsx"


def test_studio_header_imports_token_meter() -> None:
    src = CENTER_JSX.read_text(encoding="utf-8")
    assert "TokenMeter" in src


def test_studio_meter_is_read_only() -> None:
    """Read-only meter: onCompact must be null (no compact button on sessions)."""
    src = CENTER_JSX.read_text(encoding="utf-8")
    assert "onCompact={null}" in src or "onCompact=null" in src


def test_embedded_stream_has_no_token_meter_chrome() -> None:
    """The embedded live stream is pure content — no TokenMeter in its body (#10)."""
    src = SESSION_JSX.read_text(encoding="utf-8")
    assert "window.TokenMeter" not in src
