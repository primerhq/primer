"""Shared inbound resolver: route a channel message to a chat or a session gate.

One method, :meth:`ChannelInboundRouter.route`, sits behind every adapter's
inbound chat-surface handler. It resolves the durable
:class:`~primer.channel.correlation.ChannelCorrelation` for the message anchor
and either:

* opens a new thread-chat (thread channel, top-level message, chats enabled),
* publishes a session-gate resume event (``kind="session"``),
* delivers the message to the bound chat (``kind="chat"``), or
* falls back to the single-type active chat (``is_thread_channel=False``).

Side-effects only; returns None.
"""

from __future__ import annotations

import logging

from primer.channel.chat_router import ChatChannelRouter
from primer.channel.correlation import CorrelationStore
from primer.int.storage_provider import StorageProvider
from primer.model.channel import Channel


logger = logging.getLogger(__name__)


class ChannelInboundRouter:
    """Resolve an inbound chat-surface message to its destination + act."""

    def __init__(
        self,
        storage_provider: StorageProvider,
        correlation_store: CorrelationStore,
        event_bus=None,
        claim_engine=None,
        gate_inbox=None,
    ) -> None:
        self._sp = storage_provider
        self._correlation = correlation_store
        self._bus = event_bus
        self._claim_engine = claim_engine
        self._gate_inbox = gate_inbox

    def _chat_router(self) -> ChatChannelRouter:
        return ChatChannelRouter(
            storage_provider=self._sp,
            correlation_store=self._correlation,
            event_bus=self._bus,
            gate_inbox=self._gate_inbox,
            claim_engine=self._claim_engine,
        )

    async def route(
        self,
        *,
        channel: Channel,
        anchor: str | None,
        reply_to: str | None = None,
        is_thread_channel: bool,
        sender: str,
        text: str,
        media_parts: list | None = None,
    ) -> None:
        """Route one inbound chat-surface message. Side-effects only.

        ``anchor`` is the message's resolution key:

        * thread channel, in-thread message: the existing thread id;
        * thread channel, top-level message: ``None`` (a brand-new thread).
          The caller passes the NEW thread anchor in ``reply_to`` so a fresh
          thread-chat can be keyed on it;
        * single-type channel: the gate/reply target id (or ``None`` -> the
          active chat).
        """
        # Thread channel, top-level message -> open a fresh thread-chat keyed
        # on the new thread anchor (passed by the caller as ``reply_to``).
        # Ignore when chats are disabled on the room.
        if is_thread_channel and anchor is None:
            new_thread = reply_to
            if not new_thread:
                logger.warning(
                    "inbound: thread channel %s top-level message with no "
                    "new-thread anchor; ignoring", channel.id,
                )
                return
            await self.open_thread_chat(
                channel=channel, thread_external_id=new_thread,
                sender=sender, text=text, media_parts=media_parts,
            )
            return

        record = await self._correlation.lookup(channel.id, anchor) if anchor else None

        if record is not None and record.kind == "session":
            if self._bus is None:
                logger.warning(
                    "inbound: session correlation for %s but no event bus; "
                    "dropping reply", channel.id,
                )
                return
            event_key = f"ask_user:{record.session_id}:{record.tool_call_id}"
            await self._bus.publish(event_key, {"response": text})
            return

        if record is not None and record.kind == "chat":
            await self._chat_router().deliver_message(
                channel_id=channel.id, thread_external_id=anchor,
                supports_threads=is_thread_channel, sender_name=sender,
                text=text, media_parts=media_parts,
            )
            return

        # No correlation record for this anchor.
        if is_thread_channel:
            # Unknown in-thread anchor on a thread channel: ignore (we only
            # open chats from a top-level message via the explicit path below).
            logger.info(
                "inbound: unknown thread anchor %r on channel %s; ignoring",
                anchor, channel.id,
            )
            return

        # Single-type channel: route to the active chat (resolve-or-create).
        await self._chat_router().deliver_message(
            channel_id=channel.id, thread_external_id=None,
            supports_threads=False, sender_name=sender, text=text,
            media_parts=media_parts,
        )

    async def open_thread_chat(
        self,
        *,
        channel: Channel,
        thread_external_id: str,
        sender: str,
        text: str,
        media_parts: list | None = None,
    ) -> None:
        """Open (or resolve) the thread-chat for a thread channel's top-level
        message and deliver the message to it. Ignores when chats are disabled
        on the room."""
        if not channel.config.chats.enabled:
            logger.info(
                "inbound: chats disabled on channel %s; ignoring top-level "
                "message", channel.id,
            )
            return
        await self._chat_router().deliver_message(
            channel_id=channel.id, thread_external_id=thread_external_id,
            supports_threads=True, sender_name=sender, text=text,
            media_parts=media_parts,
        )


__all__ = ["ChannelInboundRouter"]
