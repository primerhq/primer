"""Terminal + events sidebar (wiring plan P2 T8)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
TERM = (CONSOLE / "nv-terminal.jsx").read_text(encoding="utf-8")
EVENTS = (CONSOLE / "nv-events-sidebar.jsx").read_text(encoding="utf-8")
CHROME = (CONSOLE / "nv-chrome.jsx").read_text(encoding="utf-8")
API = (ROOT / "ui" / "components" / "shell" / "sh-api.jsx").read_text(
    encoding="utf-8")


def test_terminal_rides_the_pty_websocket():
    assert "/terminal" in TERM
    assert "window.Terminal" in TERM and "FitAddon" in TERM
    assert "resize" in TERM
    assert 'data-testid="nv-terminal-denied"' in TERM


def test_toggle_order_terminal_before_events():
    t = CHROME.index("nv-toggle-terminal")
    e = CHROME.index("nv-toggle-events")
    assert t < e


def test_events_tail_is_cursor_paged_and_workspace_scoped():
    assert "afterId" in EVENTS and "workspaceId: con.wid" in EVENTS
    assert "max_id" in EVENTS


def test_events_locked_state_until_p6():
    m = re.search(r'status === 403[\s\S]{0,200}', EVENTS)
    assert m and "setLocked" in m.group(0)
    assert 'data-testid="nv-events-locked"' in EVENTS


def test_streaming_optin_writes_the_workspace_config():
    assert "setWorkspaceEvents" in EVENTS
    assert re.search(r'setWorkspaceEvents: function \(wid, enabled\)', API)
