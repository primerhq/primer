"""Agent form shows compaction-prompt help text."""

from __future__ import annotations

from pathlib import Path


JSX = Path(__file__).resolve().parents[2] / "ui" / "components" / "agents.jsx"


def test_help_text_present() -> None:
    src = JSX.read_text(encoding="utf-8")
    assert "Leave blank to use the default prompt" in src
    assert "preserve system context" in src
