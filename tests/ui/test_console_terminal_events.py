"""Terminal panel (wiring plan P2 T8).

The workspace events sidebar (nv-events-sidebar.jsx: streaming opt-in,
cursor-paged tail, role-denial fallback) retired in the uiv2 US-011a
cutover - the toggle, the mount and its dedicated sh-api.jsx helper
(setWorkspaceEvents) are gone with it. The terminal panel is unrelated
and survives untouched.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
TERM = (CONSOLE / "nv-terminal.jsx").read_text(encoding="utf-8")


def test_terminal_rides_the_pty_websocket():
    assert "/terminal" in TERM
    assert "window.Terminal" in TERM and "FitAddon" in TERM
    assert "resize" in TERM
    assert 'data-testid="nv-terminal-denied"' in TERM
