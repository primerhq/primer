"""Static JSX checks — session-detail.jsx wires read-only TokenMeter."""

from __future__ import annotations

from pathlib import Path


SESSION_JSX = Path(__file__).resolve().parents[2] / "ui" / "components" / "session-detail.jsx"


def test_session_imports_token_meter() -> None:
    src = SESSION_JSX.read_text(encoding="utf-8")
    assert "TokenMeter" in src


def test_session_meter_is_read_only() -> None:
    """Read-only meter: onCompact must be null (no compact button on sessions)."""
    src = SESSION_JSX.read_text(encoding="utf-8")
    assert "onCompact={null}" in src or "onCompact=null" in src
