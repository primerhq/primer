"""Message + tag helpers for the Telegram adapter."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

from matrix.channel.adapter import PromptEnvelope


ASK_TOKEN_RE = re.compile(r"\[matrix:([A-Za-z0-9_-]{16})\]")
REJECT_TOKEN_RE = re.compile(r"\[matrix:reject:([A-Za-z0-9_-]{16})\]")


def compute_tag(
    *, workspace_id: str, session_id: str, tool_call_id: str,
) -> str:
    """Deterministic 16-char base64url tag from the three IDs."""
    raw = f"{workspace_id}|{session_id}|{tool_call_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()[:12]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_ask_user_message(
    *, chat_id: str, envelope: PromptEnvelope,
) -> dict[str, Any]:
    tag = compute_tag(
        workspace_id=envelope.workspace_id,
        session_id=envelope.session_id,
        tool_call_id=envelope.tool_call_id,
    )
    text = (
        f"{envelope.prompt}\n\n"
        "Reply to this message to answer.\n"
        f"[matrix:{tag}]"
    )
    return {"chat_id": int(chat_id), "text": text}


def build_tool_approval_message(
    *, chat_id: str, envelope: PromptEnvelope,
) -> dict[str, Any]:
    tag = compute_tag(
        workspace_id=envelope.workspace_id,
        session_id=envelope.session_id,
        tool_call_id=envelope.tool_call_id,
    )
    text = f"{envelope.prompt}\n[matrix:{tag}]"
    return {
        "chat_id": int(chat_id),
        "text": text,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "Approve", "callback_data": f"a:{tag}"},
                {"text": "Reject",  "callback_data": f"r:{tag}"},
            ]],
        },
    }


def build_rejection_prompt(*, tag: str) -> dict[str, Any]:
    """The force_reply follow-up that collects a rejection reason."""
    return {
        "text": (
            "Why are you rejecting?\n"
            f"[matrix:reject:{tag}]"
        ),
        "reply_markup": {"force_reply": True, "selective": True},
    }


__all__ = [
    "ASK_TOKEN_RE",
    "REJECT_TOKEN_RE",
    "build_ask_user_message",
    "build_rejection_prompt",
    "build_tool_approval_message",
    "compute_tag",
]
