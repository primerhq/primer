"""Bridge channel gate replies into the chat soft-yield resume path.

A chat does NOT park; its pending gate is resolved by the next user_message
(the resume path in primer.chat.dispatch). So a channel gate reply is just an
appended user_message + a claimable flip; the worker's resume logic
(_find_resume_reply -> ChatTurnRunner.resume_pending) consumes it. This is the
chat analogue of ChannelInbox, but it targets chats, NOT the session bus.
"""

from __future__ import annotations

import logging

from primer.chat.enqueue import append_user_message
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
from primer.model.chat import TextPart
from primer.model.chats import Chat


logger = logging.getLogger(__name__)


class ChatResponseInbox:
    """Fan-in for channel gate replies on the chat surface."""

    def __init__(
        self, *, storage_provider: StorageProvider, event_bus: EventBus,
        claim_engine=None,
    ) -> None:
        self._sp = storage_provider
        self._bus = event_bus
        self._claim_engine = claim_engine

    async def _append_and_claim(self, *, chat_id: str, text: str) -> None:
        chat = await self._sp.get_storage(Chat).get(chat_id)
        if chat is None:
            logger.warning("chat %s vanished before gate resume", chat_id)
            return
        await append_user_message(
            chat=chat, parts=[TextPart(text=text)], storage_provider=self._sp)
        latest = await self._sp.get_storage(Chat).get(chat_id)
        if latest is not None:
            latest.turn_status = "claimable"
            await self._sp.get_storage(Chat).update(latest)
        await self._bus.publish("chat-claimable", {"chat_id": chat_id})
        if self._claim_engine is not None:
            from primer.int.claim import ClaimKind
            await self._claim_engine.upsert(ClaimKind.CHAT, chat_id, priority=10)

    async def handle_chat_response(
        self, *, chat_id: str, pending: dict, text: str, sender: str,
    ) -> None:
        """ask_user reply: the text becomes the tool_result on resume."""
        await self._append_and_claim(chat_id=chat_id, text=text)

    async def handle_chat_decision(
        self, *, chat_id: str, pending: dict, decision: str,
        reason: str | None, sender: str,
    ) -> None:
        """approval button: map to the yes/no token resume_pending parses."""
        text = "yes" if decision == "approved" else "no"
        await self._append_and_claim(chat_id=chat_id, text=text)


__all__ = ["ChatResponseInbox"]
