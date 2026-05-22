"""Per-chat turn runner — drives one user_message → assistant reply.

Lightweight stub for the M6 chat surface. The real LLM-driven
executor (mirroring matrix.agent.executor for chat semantics) is a
future deliverable; this stub establishes the WS protocol so the
operator console + integration tests work end-to-end against a
predictable backend.

The runner exposes one method, :meth:`run_turn`, which:

1. Persists the user_message row.
2. Emits a sequence of assistant_token / tool_call / tool_result /
   done rows for the configured stub script (default: a single
   ``"(stub) heard: <input>"`` token + done).
3. If the script tells it to yield, parks the chat row and emits a
   ``yielded`` marker — the WS endpoint holds the connection.

Hooks for the real executor:

* Replace :meth:`run_turn` with the actual agent loop. Everything
  else (model + storage + protocol) stays.
* The yield path already works — when the agent invokes a yielding
  tool, the resulting park goes through the same M1-M5 machinery
  whether the row lives in ``sessions`` or ``chats``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from matrix.int.storage import Storage
from matrix.model.chats import Chat, ChatMessage


logger = logging.getLogger(__name__)


class ChatTurnRunner:
    """Drive one user_message → assistant_reply round-trip.

    Constructed per request (cheap — holds storage handles only).
    The WS endpoint instantiates one when a user_message arrives,
    iterates over :meth:`run_turn` to stream rows to the client.
    """

    def __init__(
        self,
        *,
        chat_storage: Storage[Chat],
        message_storage: Storage[ChatMessage],
    ) -> None:
        self._chats = chat_storage
        self._messages = message_storage

    async def run_turn(
        self, chat: Chat, user_text: str,
    ) -> AsyncIterator[ChatMessage]:
        """Persist + stream rows for one chat turn.

        Yields each :class:`ChatMessage` immediately after it has
        been written to storage so the WS layer can forward it
        without re-reading. Storage write happens first so a
        client disconnect mid-stream still leaves the row durable.
        """
        # 1) user_message row
        user_msg = await self._append(
            chat,
            kind="user_message",
            payload={"content": user_text},
        )
        yield user_msg

        # 2) stub assistant turn — single token + done. Real executor
        # would loop the LLM here, emit assistant_token deltas, dispatch
        # tool_calls, etc.
        token_text = f"(stub) heard: {user_text}"
        assistant_msg = await self._append(
            chat,
            kind="assistant_token",
            payload={"delta": token_text},
        )
        yield assistant_msg

        done_msg = await self._append(
            chat,
            kind="done",
            payload={},
        )
        yield done_msg

    async def _append(
        self,
        chat: Chat,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> ChatMessage:
        """Persist one chat_message row + bump the chat's last_seq."""
        next_seq = chat.last_seq + 1
        row = ChatMessage(
            id=ChatMessage.make_id(chat.id, next_seq),
            chat_id=chat.id,
            seq=next_seq,
            kind=kind,  # type: ignore[arg-type]
            payload=payload,
            created_at=datetime.now(timezone.utc),
        )
        await self._messages.create(row)
        chat.last_seq = next_seq
        await self._chats.update(chat)
        return row


__all__ = ["ChatTurnRunner"]
