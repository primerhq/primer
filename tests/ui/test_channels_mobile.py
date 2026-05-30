"""channels.jsx: CardList for providers/channels/associations on
mobile + Fab for 'New' on each list."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "channels.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_use_viewport() -> None:
    assert "useViewport" in _src()


def test_card_list() -> None:
    assert "CardList" in _src()


def test_fab() -> None:
    src = _src()
    assert "Fab" in src
    assert "New channel" in src or "New channel provider" in src
