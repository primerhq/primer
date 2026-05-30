"""workspaces/providers.jsx renders CardList + Fab on mobile."""
from __future__ import annotations
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "workspaces" / "providers.jsx"
)

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_use_viewport() -> None:
    assert "useViewport" in _src()

def test_card_list() -> None:
    assert "CardList" in _src()

def test_fab() -> None:
    src = _src()
    assert "Fab" in src
    assert "New provider" in src or "New workspace provider" in src
