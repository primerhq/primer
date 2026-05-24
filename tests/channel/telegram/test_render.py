"""Renderers for Telegram messages + tag computation."""

from __future__ import annotations

import re

from matrix.channel.adapter import PromptEnvelope
from matrix.channel.telegram.render import (
    ASK_TOKEN_RE, REJECT_TOKEN_RE,
    build_ask_user_message, build_tool_approval_message,
    compute_tag,
)


def _env(kind: str = "ask_user") -> PromptEnvelope:
    return PromptEnvelope(
        kind=kind, workspace_id="ws1", session_id="s1",
        tool_call_id="tc1", prompt="please answer",
        response_schema=None,
        choices=["Approve", "Reject"] if kind == "tool_approval" else None,
        timeout_at_iso=None,
    )


def test_compute_tag_is_deterministic_and_short():
    t1 = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc")
    t2 = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc")
    assert t1 == t2
    assert len(t1) == 16
    assert re.match(r"^[A-Za-z0-9_-]{16}$", t1)


def test_different_inputs_yield_different_tags():
    a = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc")
    b = compute_tag(workspace_id="ws", session_id="s", tool_call_id="OTHER")
    assert a != b


def test_ask_user_message_includes_token_at_end():
    body = build_ask_user_message(chat_id="-100123", envelope=_env("ask_user"))
    assert body["chat_id"] == -100123
    assert body["text"].rstrip().endswith("]")
    assert ASK_TOKEN_RE.search(body["text"])
    assert body.get("reply_markup") is None  # no inline keyboard


def test_tool_approval_message_has_two_buttons_with_short_callback_data():
    body = build_tool_approval_message(
        chat_id="-100123", envelope=_env("tool_approval"),
    )
    keyboard = body["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 1 and len(keyboard[0]) == 2
    callback_a, callback_r = (b["callback_data"] for b in keyboard[0])
    assert callback_a.startswith("a:") and callback_r.startswith("r:")
    # Telegram's 64-byte ceiling — assert well within.
    assert len(callback_a.encode("utf-8")) <= 64
    assert len(callback_r.encode("utf-8")) <= 64


def test_token_regexes_match_emitted_messages():
    ask = build_ask_user_message(chat_id="1", envelope=_env("ask_user"))
    m = ASK_TOKEN_RE.search(ask["text"])
    assert m and len(m.group(1)) == 16
    # And the reject regex matches a synthesised reject prompt.
    reject_prompt = (
        "Why are you rejecting?\n"
        f"[matrix:reject:{compute_tag(workspace_id='w', session_id='s', tool_call_id='tc')}]"
    )
    assert REJECT_TOKEN_RE.search(reject_prompt)
