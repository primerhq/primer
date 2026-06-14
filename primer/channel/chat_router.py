"""Resolve-or-create the Chat bound to a channel (thread) for inbound routing.

Multi-type: a thread maps 1:1 to a Chat whose channel_binding ==
(channel_id, thread_id). Single-type: the active-chat correlation record
(``ACTIVE_CHAT_ANCHOR``) holds the current 1:1 chat id.

The room's default agent + chat-enablement come from the room ``Channel``'s
``config.chats`` (``ChatConfig``); routing/active-chat state lives in the
``CorrelationStore`` rather than a per-channel association row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from primer.chat.enqueue import append_user_message
from primer.channel.correlation import ACTIVE_CHAT_ANCHOR, CorrelationStore
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
from primer.model.agent import Agent
from primer.model.channel import Channel
from primer.model.chat import TextPart
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.except_ import NotFoundError
from primer.model.storage import OffsetPage


class ChatChannelRouter:
    """Maps inbound channel messages to their bound Chat."""

    def __init__(
        self, *, storage_provider: StorageProvider,
        correlation_store: CorrelationStore | None = None,
        event_bus: "EventBus | None" = None, gate_inbox=None,
        claim_engine=None,
    ) -> None:
        self._sp = storage_provider
        self._correlation = correlation_store or CorrelationStore(storage_provider)
        self._bus = event_bus
        self._gate_inbox = gate_inbox
        self._claim_engine = claim_engine

    async def _default_agent_id(self, channel_id: str) -> str:
        """Resolve the room's default agent from ``Channel.config.chats``.

        Raises ``NotFoundError`` when the channel is unknown, ``ValueError``
        when chats are disabled or no default_agent is configured."""
        channel = await self._sp.get_storage(Channel).get(channel_id)
        if channel is None:
            raise NotFoundError(f"no Channel {channel_id!r}")
        chats = channel.config.chats
        if not chats.enabled:
            raise ValueError(f"chats are disabled on channel {channel_id!r}")
        if not chats.default_agent:
            raise ValueError(f"no default_agent configured on channel {channel_id!r}")
        return chats.default_agent

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
        """Resolve the live Chat bound to ``(channel_id, thread_external_id)``.

        Fast path: a :class:`CorrelationStore` record keyed on the thread
        anchor (written by :meth:`resolve_or_create` when the thread's chat is
        created) maps the thread directly to its chat id, so the common case
        is a single keyed lookup + a single get -- no table scan.

        Slow path (legacy / anomaly): when no correlation record exists, or it
        points at a chat that has been deleted, no longer carries the matching
        binding, or has ``status == 'ended'``, fall back to the full scan that
        this method used historically and refresh the correlation to the live
        chat it finds. The return value is therefore IDENTICAL to the old scan
        for every case: the fast path only short-circuits when it has a live
        chat whose binding matches (which the scan would also have returned),
        and every other case defers to the scan itself.
        """
        chats = self._sp.get_storage(Chat)
        # Fast path: keyed correlation lookup (thread external id is the anchor).
        record = await self._correlation.lookup(channel_id, thread_external_id)
        if record is not None and record.chat_id is not None:
            candidate = await chats.get(record.chat_id)
            if (
                candidate is not None
                and candidate.status != "ended"
                and candidate.channel_binding is not None
                and candidate.channel_binding.channel_id == channel_id
                and candidate.channel_binding.thread_external_id
                == thread_external_id
            ):
                return candidate
        # Slow path: scan (covers legacy chats with no correlation record, an
        # ended/mismatched correlated chat, etc.).  Refresh the correlation so
        # the next lookup hits the fast path.
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
                    and c.status != "ended"
                ):
                    await self._correlation.upsert_chat(
                        channel_id=channel_id, anchor=thread_external_id,
                        chat_id=c.id)
                    return c
            if len(page.items) < 200:
                return None
            offset += 200

    async def resolve_or_create(
        self, *, channel_id: str, thread_external_id: str | None,
        supports_threads: bool,
    ) -> tuple[Chat, bool]:
        """Return (chat, created). created=True when a fresh chat was made."""
        agent_id = await self._default_agent_id(channel_id)
        if supports_threads:
            if thread_external_id is not None:
                existing = await self._find_thread_chat(
                    channel_id=channel_id, thread_external_id=thread_external_id)
                if existing is not None and existing.status != "ended":
                    return existing, False
            chat = await self._new_chat(
                agent_id=agent_id, channel_id=channel_id,
                thread_external_id=thread_external_id)
            # Record the thread->chat correlation so inbound routing can find
            # this chat by its thread anchor on later messages.
            if thread_external_id is not None:
                await self._correlation.upsert_chat(
                    channel_id=channel_id, anchor=thread_external_id,
                    chat_id=chat.id)
            return chat, True
        # single-type: the active-chat correlation tracks the current chat.
        active = await self._correlation.lookup(channel_id, ACTIVE_CHAT_ANCHOR)
        if active is not None and active.chat_id is not None:
            current = await self._sp.get_storage(Chat).get(active.chat_id)
            if current is not None and current.status != "ended":
                return current, False
        chat = await self._new_chat(
            agent_id=agent_id, channel_id=channel_id,
            thread_external_id=None)
        await self._correlation.set_active_chat(channel_id, chat.id)
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
