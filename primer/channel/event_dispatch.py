"""ChannelEventRouter: correlation-first inbound precedence over channel triggers.

A normalized :class:`~primer.model.channel_event.ChannelEvent` arrives from a
provider's inbound gateway and is routed here. The precedence is:

  1. Correlation-first. When the event carries a ``thread_anchor`` and a durable
     :class:`~primer.model.channel_correlation.ChannelCorrelation` exists for
     ``(channel_id, thread_anchor)``, the event is a reply to a known artefact:

       * ``kind="session"`` -> publish ``ask_user:{sid}:{tcid}`` so the parked
         session gate resumes (mirrors the legacy
         :class:`~primer.channel.inbound_router.ChannelInboundRouter` gate path).
       * ``kind="chat"``    -> deliver the message to the bound chat via
         :class:`~primer.channel.chat_router.ChatChannelRouter`.

     Either way the router returns: a correlated reply NEVER fans out to
     channel triggers.

  2. Otherwise it is a fresh inbound event. Resolve every ``kind="channel"``
     :class:`~primer.model.trigger.Trigger` whose ``provider_id`` matches and
     whose ``channel_id`` is either unset (provider-wide) or equal to this
     channel, and fire each via the injected ``fire_trigger`` with the event in
     ``extra_context["event"]``. Per-trigger failures are isolated.

Side-effects only; returns ``None``.
"""

from __future__ import annotations

import logging

from primer.channel.chat_router import ChatChannelRouter
from primer.channel.correlation import CorrelationStore
from primer.int.storage_provider import StorageProvider
from primer.model.channel import Channel
from primer.model.channel_event import ChannelEvent
from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Trigger
from primer.storage.q import Q
from primer.trigger.dispatch import fire_trigger as _default_fire_trigger
from primer.trigger.subscribers import DispatchDeps


logger = logging.getLogger(__name__)


class ChannelEventRouter:
    """Route a normalized ``ChannelEvent`` correlation-first, else fire rules."""

    def __init__(
        self,
        *,
        storage_provider: StorageProvider,
        correlation_store: CorrelationStore,
        fire_deps: DispatchDeps,
        event_bus=None,
        fire_trigger=_default_fire_trigger,
    ) -> None:
        self._sp = storage_provider
        self._correlation = correlation_store
        self._fire_deps = fire_deps
        self._bus = event_bus
        self._fire_trigger = fire_trigger

    def _chat_router(self) -> ChatChannelRouter:
        return ChatChannelRouter(
            storage_provider=self._sp,
            correlation_store=self._correlation,
            event_bus=self._bus,
        )

    async def route_event(
        self, *, event: ChannelEvent, channel: Channel | None
    ) -> None:
        """Route one normalized inbound event. Side-effects only."""
        channel_id = event.channel_id or (channel.id if channel is not None else None)

        # The SDK-free normalizers only set ``room_external_id`` (the platform
        # room id), not the internal ``channel_id``. Stamp the resolved internal
        # id onto the event so downstream subscribers - notably ``start_chat`` -
        # can build a ChatChannelBinding the outbound relay resolves back to
        # this channel's adapter. Without it the bound chat has no reply route.
        if channel is not None and not event.channel_id:
            event.channel_id = channel.id

        # ----- (1) Correlation-first -----------------------------------
        if event.thread_anchor and channel_id is not None:
            record = await self._correlation.lookup(channel_id, event.thread_anchor)
            if record is not None and record.kind == "session":
                if self._bus is None:
                    logger.warning(
                        "channel event: session correlation for %s but no "
                        "event bus; dropping reply", channel_id,
                    )
                    return
                event_key = f"ask_user:{record.session_id}:{record.tool_call_id}"
                await self._bus.publish(event_key, {"response": event.text})
                return
            if record is not None and record.kind == "chat":
                await self._chat_router().deliver_message(
                    channel_id=channel_id,
                    thread_external_id=event.thread_anchor,
                    supports_threads=event.surface == "thread",
                    sender_name=event.sender.display_name
                    or event.sender.external_id,
                    text=event.text or "",
                    media_parts=None,
                )
                return

        # ----- (2) Fresh event -> fire channel triggers ----------------
        triggers = await self._resolve_channel_triggers(
            event.provider_id, channel_id,
        )
        extra_context = {"event": event.model_dump(mode="json")}
        for trigger in triggers:
            try:
                await self._fire_trigger(
                    trigger_id=trigger.id,
                    scheduled_for=None,
                    deps=self._fire_deps,
                    extra_context=extra_context,
                )
            except Exception:  # noqa: BLE001 -- isolate per-trigger failures
                logger.exception(
                    "channel event: fire_trigger raised for %s", trigger.id,
                )

    async def _resolve_channel_triggers(
        self, provider_id: str, channel_id: str | None,
    ) -> list[Trigger]:
        """Page ``kind="channel"`` triggers for *provider_id*, keeping those
        whose ``channel_id`` is unset (provider-wide) or equal to *channel_id*."""
        storage = self._sp.get_storage(Trigger)
        q = Q(Trigger).where_op("config.kind", Op.EQ, "channel")
        matched: list[Trigger] = []
        offset = 0
        while offset < 10_000:
            page = await storage.find(
                q.build(), OffsetPage(offset=offset, length=200),
            )
            for trigger in page.items:
                cfg = trigger.config
                if getattr(cfg, "provider_id", None) != provider_id:
                    continue
                cfg_channel = getattr(cfg, "channel_id", None)
                if cfg_channel is None or cfg_channel == channel_id:
                    matched.append(trigger)
            if len(page.items) < 200:
                break
            offset += 200
        return matched


__all__ = ["ChannelEventRouter"]
