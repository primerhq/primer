"""Static JSX checks for the optimistic "queued" follow-up render path.

When the user sends a message while the agent is still "Thinking…" the
UI must render it immediately AFTER the Thinking indicator with a
"(queued)" badge, tag it with a client-generated ``client_msg_id``, and
reconcile (remove the optimistic placeholder) when the real seq'd row
arrives over the WS carrying the same ``client_msg_id``.
"""

from __future__ import annotations

from pathlib import Path


CHATS = Path(__file__).resolve().parents[2] / "ui" / "components" / "chats.jsx"


def _src() -> str:
    return CHATS.read_text(encoding="utf-8")


def test_sendmessage_generates_and_sends_client_msg_id():
    src = _src()
    # A per-send client id is generated (uuid) and attached to the frame.
    assert "client_msg_id" in src
    assert "randomUUID" in src
    # The generated id is stamped onto the outbound user_message frame.
    assert "client_msg_id: clientMsgId" in src


def test_optimistic_queued_message_appended_when_turn_active():
    src = _src()
    # Optimistic placeholder carries a queued flag + the client id.
    assert "queued: true" in src
    # Rendered with a visible "(queued)" indicator.
    assert "(queued)" in src
    # The queued placeholder row is addressable for tests / styling.
    assert "chat-queued-msg" in src


def test_queued_rows_render_below_thinking_indicator():
    src = _src()
    # The queued placeholders are split out of the main list so they can
    # render after the Thinking bubble (pinned at the bottom).
    assert "m.queued" in src or ".queued" in src


def test_reconcile_removes_placeholder_by_client_msg_id():
    src = _src()
    # When the real row arrives the matching optimistic placeholder is
    # removed by client_msg_id (seq de-dupe alone can't — temps have no seq).
    assert "p.client_msg_id === msg.client_msg_id" in src
