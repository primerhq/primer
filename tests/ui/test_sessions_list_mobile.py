"""sessions-list.jsx uses useViewport(), wraps the table with a CardList
on mobile, and renders a Fab for the 'New session' affordance."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "sessions-list.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_use_viewport() -> None:
    assert "useViewport" in _src()


def test_card_list_branch() -> None:
    assert "CardList" in _src()


def test_fab_present_when_new_session_affordance_exists() -> None:
    src = _src()
    assert "Fab" in src or "no-fab-justification" in src
