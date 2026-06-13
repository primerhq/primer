"""remember_reply_target round-trips (the Telegram reject-with-reason flow)."""

from __future__ import annotations

from types import SimpleNamespace

from primer.channel.telegram.adapter import TelegramChannelAdapter


def _adapter():
    return TelegramChannelAdapter(
        provider=SimpleNamespace(id="cp"),
        channel=SimpleNamespace(id="ch-1", external_id="555"), inbox=None)


def test_remember_then_resolve_reply_target():
    a = _adapter()
    ids = {"workspace_id": "ws-1", "session_id": "s-1", "tool_call_id": "tc-1"}
    a.remember_reply_target(message_id=42, ids=ids, kind="reject")
    got = a.resolve_reply_target(42)
    assert got == {**ids, "kind": "reject"}


def test_resolve_unknown_reply_target_is_none():
    assert _adapter().resolve_reply_target(999) is None
