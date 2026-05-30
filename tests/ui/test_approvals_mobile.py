"""approvals.jsx: CardList on mobile; BottomSheet wraps the
approve/deny action panel."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "approvals.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_use_viewport() -> None:
    assert "useViewport" in _src()


def test_card_list() -> None:
    assert "CardList" in _src()


def test_bottom_sheet_for_actions() -> None:
    assert "BottomSheet" in _src()
