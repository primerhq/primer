"""The chat feature applies a mobile layout: useViewport(), sticky
composer, back-arrow header, kebab actions menu, BottomSheet for tool/
approval drawers.

Task B2 (chat-refactor plan) moved the composer (and its mobile sticky
styling) out of ChatDetail (chats.jsx) into the embeddable
<Conversation> core (chat/conversation.jsx); the mobile header + kebab
sheet stayed in chats.jsx as host page chrome. Read both files so these
assertions keep validating the same behavioral contract regardless of
which file a given piece lives in.
"""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "chats.jsx"
CONVERSATION_SRC = (
    Path(__file__).resolve().parents[2] / "ui" / "components" / "chat" / "conversation.jsx"
)


def _src() -> str:
    return SRC.read_text(encoding="utf-8") + "\n" + CONVERSATION_SRC.read_text(encoding="utf-8")


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
