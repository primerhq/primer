"""Static JSX checks — the chat feature wires the TokenMeter + compact
handler.

Task B2 (chat-refactor plan) split this across two files: ChatDetail
(chats.jsx) renders the TokenMeter as page chrome, while the WS usage/
compaction envelope handling + the /compact POST now live in the
embeddable <Conversation> core (chat/conversation.jsx), reported back
to the host via the onStatus callback. Read both so these assertions
keep validating the same behavioral contract.
"""

from __future__ import annotations

from pathlib import Path


CHATS_JSX = Path(__file__).resolve().parents[2] / "ui" / "components" / "chats.jsx"
CONVERSATION_JSX = (
    Path(__file__).resolve().parents[2] / "ui" / "components" / "chat" / "conversation.jsx"
)


def _src() -> str:
    return (
        CHATS_JSX.read_text(encoding="utf-8")
        + "\n"
        + CONVERSATION_JSX.read_text(encoding="utf-8")
    )


def test_chats_imports_token_meter() -> None:
    assert "TokenMeter" in _src()


def test_chats_handles_usage_envelope() -> None:
    src = _src()
    assert '"usage"' in src or "'usage'" in src


def test_chats_handles_compaction_envelope() -> None:
    src = _src()
    assert '"compaction"' in src or "'compaction'" in src


def test_chats_compact_post_handler() -> None:
    assert "/compact" in _src()
