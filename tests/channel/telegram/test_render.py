"""Renderers for Telegram messages + tag computation (HTML, tokenless)."""

from __future__ import annotations

import re

from primer.channel.adapter import PromptEnvelope
from primer.channel.telegram.render import (
    build_ask_user_message,
    build_rejection_prompt,
    build_tool_approval_message,
    compute_tag,
)


def _env(kind: str = "ask_user") -> PromptEnvelope:
    return PromptEnvelope(
        kind=kind, workspace_id="ws1", session_id="s1",
        tool_call_id="tc1", prompt="please answer",
        response_schema=None,
        choices=["Approve", "Reject"] if kind == "tool_approval" else None,
        timeout_at_iso=None,
        tool_name="write" if kind == "tool_approval" else None,
        tool_args={"path": "a.txt", "content": "hi"} if kind == "tool_approval" else None,
    )


def test_compute_tag_is_deterministic_and_short():
    t1 = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc")
    t2 = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc")
    assert t1 == t2
    assert len(t1) == 16
    assert re.match(r"^[A-Za-z0-9_-]{16}$", t1)


def test_ask_user_message_is_html_and_tokenless():
    body = build_ask_user_message(chat_id="-100123", envelope=_env("ask_user"))
    assert body["chat_id"] == -100123
    assert body["parse_mode"] == "HTML"
    assert "[primer:" not in body["text"]
    assert "please answer" in body["text"]
    assert "Reply to this message" in body["text"]
    assert body.get("reply_markup") is None  # no inline keyboard


def test_ask_user_escapes_html_special_chars():
    env = PromptEnvelope(
        kind="ask_user", workspace_id="w", session_id="s", tool_call_id="tc",
        prompt="2 < 3 & 4 > 1", response_schema=None, choices=None,
        timeout_at_iso=None,
    )
    body = build_ask_user_message(chat_id="1", envelope=env)
    assert "&lt;" in body["text"] and "&gt;" in body["text"] and "&amp;" in body["text"]
    assert "2 < 3" not in body["text"]  # raw angle brackets escaped


def test_tool_approval_message_tokenless_with_json_args_and_buttons():
    body = build_tool_approval_message(
        chat_id="-100123", envelope=_env("tool_approval"),
    )
    assert body["parse_mode"] == "HTML"
    assert "[primer:" not in body["text"]
    assert "<b>write</b>" in body["text"]
    # args rendered as pretty JSON inside a <pre> block
    assert "<pre>" in body["text"] and '"path"' in body["text"]
    keyboard = body["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 1 and len(keyboard[0]) == 2
    callback_a, callback_r = (b["callback_data"] for b in keyboard[0])
    assert callback_a.startswith("a:") and callback_r.startswith("r:")
    assert len(callback_a.encode("utf-8")) <= 64
    assert len(callback_r.encode("utf-8")) <= 64


def test_tool_approval_falls_back_to_prompt_without_structured_args():
    env = PromptEnvelope(
        kind="tool_approval", workspace_id="w", session_id="s", tool_call_id="tc",
        prompt="Approve something?", response_schema=None,
        choices=["Approve", "Reject"], timeout_at_iso=None,
    )
    body = build_tool_approval_message(chat_id="1", envelope=env)
    assert "Approve something?" in body["text"]


def test_rejection_prompt_is_force_reply_and_tokenless():
    body = build_rejection_prompt()
    assert body["parse_mode"] == "HTML"
    assert "[primer:" not in body["text"]
    assert body["reply_markup"]["force_reply"] is True
