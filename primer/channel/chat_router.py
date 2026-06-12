"""Resolve-or-create the Chat bound to a channel (thread) for inbound routing.

Multi-type: a thread maps 1:1 to a Chat whose channel_binding ==
(channel_id, thread_id). Single-type: the channel's ChatChannelAssociation
holds active_chat_id (the current 1:1 chat).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from primer.chat.enqueue import append_user_message
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
from primer.model.agent import Agent
from primer.model.channel import ChatChannelAssociation
from primer.model.chat import TextPart
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.except_ import NotFoundError
from primer.model.storage import OffsetPage
from primer.storage.q import Q


class ChatChannelRouter:
    """Maps inbound channel messages to their bound Chat."""

    def __init__(
        self, *, storage_provider: StorageProvider,
        event_bus: "EventBus | None" = None, gate_inbox=None,
        claim_engine=None,
    ) -> None:
        self._sp = storage_provider
        self._bus = event_bus
        self._gate_inbox = gate_inbox
        self._claim_engine = claim_engine

    async def _association(self, channel_id: str) -> ChatChannelAssociation:
        page = await self._sp.get_storage(ChatChannelAssociation).find(
            Q(ChatChannelAssociation).where("channel_id", channel_id).build(),
            OffsetPage(offset=0, length=1),
        )
        if not page.items:
            raise NotFoundError(
                f"no ChatChannelAssociation for channel {channel_id!r}"
            )
        return page.items[0]

    async def _new_chat(
        self, *, agent_id: str, channel_id: str, thread_external_id: str | None,
    ) -> Chat:
        agent = await self._sp.get_storage(Agent).get(agent_id)
        if agent is None:
            raise NotFoundError(f"Agent {agent_id!r} does not exist")
        chat = Chat(
            id=f"chat-{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            created_at=datetime.now(timezone.utc),
            channel_binding=ChatChannelBinding(
                channel_id=channel_id, thread_external_id=thread_external_id,
            ),
        )
        return await self._sp.get_storage(Chat).create(chat)

    async def _find_thread_chat(
        self, *, channel_id: str, thread_external_id: str,
    ) -> Chat | None:
        chats = self._sp.get_storage(Chat)
        offset = 0
        while True:
            page = await chats.find(
                None, OffsetPage(offset=offset, length=200),
            )
            for c in page.items:
                b = c.channel_binding
                if (
                    b is not None
                    and b.channel_id == channel_id
                    and b.thread_external_id == thread_external_id
                ):
                    return c
            if len(page.items) < 200:
                return None
            offset += 200

    async def resolve_or_create(
        self, *, channel_id: str, thread_external_id: str | None,
        supports_threads: bool,
    ) -> tuple[Chat, bool]:
        """Return (chat, created). created=True when a fresh chat was made."""
        assoc = await self._association(channel_id)
        if supports_threads:
            if thread_external_id is not None:
                existing = await self._find_thread_chat(
                    channel_id=channel_id, thread_external_id=thread_external_id)
                if existing is not None and existing.status != "ended":
                    return existing, False
            chat = await self._new_chat(
                agent_id=assoc.default_agent_id, channel_id=channel_id,
                thread_external_id=thread_external_id)
            return chat, True
        # single-type: track active_chat_id on the association
        if assoc.active_chat_id is not None:
            current = await self._sp.get_storage(Chat).get(assoc.active_chat_id)
            if current is not None and current.status != "ended":
                return current, False
        chat = await self._new_chat(
            agent_id=assoc.default_agent_id, channel_id=channel_id,
            thread_external_id=None)
        assoc.active_chat_id = chat.id
        await self._sp.get_storage(ChatChannelAssociation).update(assoc)
        return chat, True

    async def deliver_message(
        self, *, channel_id: str, thread_external_id: str | None,
        supports_threads: bool, sender_name: str, text: str,
        media_parts: list | None = None,
    ) -> tuple[Chat, bool]:
        """Route an inbound chat message: resolve-or-create the chat, then
        either resolve its pending gate or append an attributed user_message
        and flip the chat claimable. Returns (chat, created).

        ``media_parts`` carries already-built media parts (image/document/
        audio) for the message; the attributed text becomes the leading
        TextPart and the media parts follow. A media reply to a pending gate
        degrades to its caption text (gates are text-only)."""
        chat, created = await self.resolve_or_create(
            channel_id=channel_id, thread_external_id=thread_external_id,
            supports_threads=supports_threads)
        if chat.pending_tool_call is not None and self._gate_inbox is not None:
            await self._gate_inbox.handle_chat_response(
                chat_id=chat.id, pending=chat.pending_tool_call,
                text=text, sender=sender_name)
            return chat, created
        attributed = f"[{sender_name}] {text}" if sender_name else text
        parts: list = []
        if text or not media_parts:
            parts.append(TextPart(text=attributed))
        parts.extend(media_parts or [])
        await append_user_message(
            chat=chat, parts=parts,
            storage_provider=self._sp)
        latest = await self._sp.get_storage(Chat).get(chat.id)
        if latest is not None:
            latest.turn_status = "claimable"
            await self._sp.get_storage(Chat).update(latest)
        if self._bus is not None:
            await self._bus.publish("chat-claimable", {"chat_id": chat.id})
        if self._claim_engine is not None:
            from primer.int.claim import ClaimKind
            await self._claim_engine.upsert(ClaimKind.CHAT, chat.id, priority=10)
        return chat, created


__all__ = ["ChatChannelRouter"]
