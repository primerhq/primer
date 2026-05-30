"""chats.jsx applies a mobile layout: useViewport(), sticky composer,
back-arrow header, kebab actions menu, BottomSheet for tool/approval
drawers."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "chats.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_use_viewport() -> None:
    assert "useViewport" in _src()


def test_chat_mobile_header_class() -> None:
    src = _src()
    assert "chat-mobile-header" in src or "chat-header-mobile" in src


def test_composer_sticky_class() -> None:
    src = _src()
    assert "composer-sticky" in src or "chat-composer-mobile" in src


def test_bottom_sheet_used_for_drawers() -> None:
    src = _src()
    assert "BottomSheet" in src
