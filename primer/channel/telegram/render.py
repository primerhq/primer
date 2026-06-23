"""Message + tag helpers for the Telegram adapter.

Messages are rendered with Telegram's HTML ``parse_mode``. Reply
correlation no longer embeds a visible ``[primer:...]`` token in the
text: text replies are matched by the id of the message they reply to
(see the adapter's reply-target cache), and the Approve/Reject buttons
carry their tag invisibly in ``callback_data``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from primer.channel.adapter import PromptEnvelope


def compute_tag(
    *, workspace_id: str, session_id: str, tool_call_id: str,
) -> str:
    """Deterministic 16-char base64url tag from the three IDs.

    Still used for the Approve/Reject button ``callback_data`` (invisible
    to the user, <= 64 bytes per Telegram's limit).
    """
    raw = f"{workspace_id}|{session_id}|{tool_call_id}".encode()
    digest = hashlib.sha256(raw).digest()[:12]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _esc(s: str) -> str:
    """Escape the three characters Telegram HTML treats specially."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_ask_user_message(
    *, chat_id: str, envelope: PromptEnvelope,
) -> dict[str, Any]:
    text = (
        "\U0001F535 <b>Primer · input needed</b>\n\n"
        f"{_esc(envelope.prompt)}\n\n"
        "<i>↳ Reply to this message to answer.</i>"
    )
    return {"chat_id": int(chat_id), "text": text, "parse_mode": "HTML"}


def build_tool_approval_message(
    *, chat_id: str, envelope: PromptEnvelope,
) -> dict[str, Any]:
    tag = compute_tag(
        workspace_id=envelope.workspace_id,
        session_id=envelope.session_id,
        tool_call_id=envelope.tool_call_id,
    )
    lines = ["\U0001F7E1 <b>Primer · approval needed</b>", ""]
    if envelope.tool_name:
        lines.append(f"Run tool <b>{_esc(envelope.tool_name)}</b>")
        if envelope.tool_args:
            pretty = json.dumps(envelope.tool_args, indent=2, ensure_ascii=False)
            lines.append(f"<pre>{_esc(pretty)}</pre>")
    else:
        # Fallback: render the pre-built prompt string.
        lines.append(_esc(envelope.prompt))
    return {
        "chat_id": int(chat_id),
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"a:{tag}"},
                {"text": "❌ Reject",  "callback_data": f"r:{tag}"},
            ]],
        },
    }


def build_rejection_prompt() -> dict[str, Any]:
    """The force_reply follow-up that collects a rejection reason.

    No visible token: the caller caches this message's id as a
    reject-kind reply target, so the user's reply is correlated by the
    message it replies to.
    """
    return {
        "text": (
            "✏️ <b>Reason for rejecting?</b>\n\n"
            "<i>↳ Reply to this message with a short reason.</i>"
        ),
        "parse_mode": "HTML",
        "reply_markup": {"force_reply": True, "selective": True},
    }


__all__ = [
    "build_ask_user_message",
    "build_rejection_prompt",
    "build_tool_approval_message",
    "compute_tag",
]
