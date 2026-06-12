"""Shared helper for resolving a chat's pending gate (ask_user / approval)
as a rejection, without needing a live ChatTurnRunner.

Used by the runner's ``abandon_pending`` (cancel-while-awaiting) and by the
agent-switch endpoint (auto-reject the pending gate before switching agents).
Keeps the append-only chat history valid: the unpaired ``tool_use`` gets a
synthetic rejected ``tool_result``, and a terminal ``cancelled`` row closes the
parked turn so the next drain advances past the prompting user_message.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from primer.model.chats import Chat, ChatMessage


async def _append_row(chat: Chat, *, kind: str, payload: dict[str, Any],
                      messages: Any) -> ChatMessage:
    next_seq = chat.last_seq + 1
    row = ChatMessage(
        id=ChatMessage.make_id(chat.id, next_seq),
        chat_id=chat.id,
        seq=next_seq,
        kind=kind,  # type: ignore[arg-type]
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )
    await messages.create(row)
    chat.last_seq = next_seq
    return row


async def abandon_pending_rows(
    chat: Chat,
    *,
    pending: dict[str, Any],
    messages: Any,
    chats: Any,
    result_text: str,
    terminal_reason: str,
) -> None:
    """Append a rejected ``tool_result`` + terminal ``cancelled`` row for the
    pending gate and clear ``chat.pending_tool_call``. Persists the chat once,
    preserving externally-updated ``cancel_requested_at`` + ``agent_id``."""
    await _append_row(chat, kind="tool_result", messages=messages, payload={
        "id": pending.get("tool_call_id"),
        "name": str(pending.get("mode") or ""),
        "result": result_text,
        "error": True,
    })
    await _append_row(chat, kind="cancelled", messages=messages, payload={
        "reason": terminal_reason,
    })
    chat.pending_tool_call = None
    latest = await chats.get(chat.id)
    if latest is not None:
        chat.cancel_requested_at = latest.cancel_requested_at
        chat.agent_id = latest.agent_id
    await chats.update(chat)
