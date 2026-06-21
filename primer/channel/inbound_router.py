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

    async def route_event(self, *, event, channel: Channel) -> None:
        """Route a normalized :class:`ChannelEvent` correlation-first, else fire
        channel triggers.

        This is the typed inbound entry for the event-driven path. The legacy
        :meth:`route` stays the fallback for the rejection-reason reply path.
        """
        from primer.channel.event_dispatch import ChannelEventRouter
        from primer.observability import metrics
        from primer.trigger.subscribers import DispatchDeps

        event_type = getattr(event.type, "value", event.type)
        provider = getattr(event.provider, "value", event.provider)
        metrics.channel_events_normalized_total.labels(
            event_type=event_type, provider=provider,
        ).inc()

        fire_deps = DispatchDeps(
            storage_provider=self._sp,
            claim_engine=self._claim_engine,
            scheduler=None,
            event_bus=self._bus,
        )
        router = ChannelEventRouter(
            storage_provider=self._sp,
            correlation_store=self._correlation,
            fire_deps=fire_deps,
            event_bus=self._bus,
        )
        matched = await self._count_matched_bindings(event=event, channel=channel)
        if matched:
            metrics.channel_events_matched_total.labels(
                event_type=event_type, provider=provider,
            ).inc()
        await router.route_event(event=event, channel=channel)
        if matched:
            metrics.channel_events_dispatched_total.labels(
                event_type=event_type, provider=provider,
            ).inc()

    async def has_matching_rule(self, *, event, channel: Channel) -> bool:
        """Read-only: does any enabled channel-trigger subscription's matcher
        match this event?

        Correlated replies never match (they belong to the chat/session
        correlation path). Adapter inbound handlers call this to decide whether
        the rule path OWNS this event (fire the rule, skip the default
        chat-surface dispatch) or not (let the chat dispatch deliver it). This
        keeps a single message from being delivered twice - once by the rule
        path and once by the legacy chat dispatch.
        """
        return await self._count_matched_bindings(event=event, channel=channel)

    async def _count_matched_bindings(self, *, event, channel: Channel) -> bool:
        """Read-only pre-pass: does any channel-trigger subscription's
        ``event_matcher`` match this event? Pure counting helper for the
        matched/dispatched metrics; never mutates state and never fires."""
        from primer.channel.event_dispatch import ChannelEventRouter
        from primer.model.event_matcher import matches as _matches
        from primer.model.storage import OffsetPage, Op
        from primer.model.trigger import Subscription
        from primer.storage.q import Q
        from primer.trigger.subscribers import DispatchDeps

        # A correlated reply never fans out to rules; no rule match in that case.
        channel_id = event.channel_id or (channel.id if channel is not None else None)
        if event.thread_anchor and channel_id is not None:
            record = await self._correlation.lookup(channel_id, event.thread_anchor)
            if record is not None:
                return False

        resolver = ChannelEventRouter(
            storage_provider=self._sp,
            correlation_store=self._correlation,
            fire_deps=DispatchDeps(
                storage_provider=self._sp,
                claim_engine=self._claim_engine,
                scheduler=None,
                event_bus=self._bus,
            ),
            event_bus=self._bus,
        )
        triggers = await resolver._resolve_channel_triggers(
            event.provider_id, channel_id,
        )
        if not triggers:
            return False
        subs_storage = self._sp.get_storage(Subscription)
        for trigger in triggers:
            q = Q(Subscription).where_op("trigger_id", Op.EQ, trigger.id)
            offset = 0
            while offset < 10_000:
                page = await subs_storage.find(
                    q.build(), OffsetPage(offset=offset, length=200),
                )
                for sub in page.items:
                    if not sub.enabled or sub.event_matcher is None:
                        continue
                    if _matches(sub.event_matcher, event):
                        return True
                if len(page.items) < 200:
                    break
                offset += 200
        return False

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
