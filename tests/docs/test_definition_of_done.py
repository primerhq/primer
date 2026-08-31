"""AGENTS.md's Definition of Done tracks the v2 subsystem set.

S9 section 5: the primectl completeness track is removed and the UI track
re-points at the fresh shell.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "AGENTS.md"


def _tracks() -> list[str]:
    text = AGENTS.read_text(encoding="utf-8")
    section = text.split("## 4. Definition of Done", 1)[1].split("\n---", 1)[0]
    return re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", section, re.MULTILINE)


def test_primectl_track_is_gone() -> None:
    assert "primectl" not in AGENTS.read_text(encoding="utf-8")


def test_track_set_is_the_v2_seven() -> None:
    assert _tracks() == [
        "Backend",
        "UI",
        "System tools",
        "Docs",
        "Unit tests",
        "E2E tests",
        "Regressions",
    ]


def test_ui_track_points_at_the_fresh_shell() -> None:
    text = AGENTS.read_text(encoding="utf-8")
    section = text.split("## 4. Definition of Done", 1)[1]
    ui = section.split("**UI**", 1)[1].split("\n3.", 1)[0]
    assert "shell" in ui.lower(), "UI track still describes the classic console"
    assert "console component" not in ui


def test_layout_section_has_no_dead_packages() -> None:
    text = AGENTS.read_text(encoding="utf-8")
    layout = text.split("- **Layout:**", 1)[1].split("## 3.", 1)[0]
    for dead in ("`chat`", "primectl", "docling"):
        assert dead not in layout, f"layout still lists {dead}"
