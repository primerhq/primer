"""S7 section 6: the Trace panel is mounted on the session panels."""
from __future__ import annotations

from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "studio-center.jsx"
)


def test_mounted_in_both_session_panels() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert src.count("window.SessionTracePanel") >= 2


def test_mount_is_guarded_on_the_global() -> None:
    """The panel ships as a separate script; a missing global must not
    blank the whole session panel."""
    src = SRC.read_text(encoding="utf-8")
    assert "window.SessionTracePanel && (" in src


def test_mount_passes_sid_turn_and_status() -> None:
    src = SRC.read_text(encoding="utf-8")
    idx = src.index("window.SessionTracePanel && (")
    frag = src[idx : idx + 400]
    assert "sid={sid}" in frag
    assert "turnNo=" in frag
    assert "sessionStatus=" in frag


def test_agent_panel_mount_follows_the_inline_yields() -> None:
    src = SRC.read_text(encoding="utf-8")
    yields_at = src.index("<ST_InlineYields")
    trace_at = src.index("window.SessionTracePanel && (")
    assert yields_at < trace_at
