"""Shared inbound resolver: route one normalized channel event.

One method, :meth:`ChannelInboundRouter.route_event`, sits behind every
adapter's inbound handler. It resolves the durable
:class:`~primer.channel.correlation.ChannelCorrelation` for the message
anchor and either resumes a parked session gate, steers the thread's mapped
session, or fires the channel triggers that create one.

There is no chat fallback: a platform thread IS a session (S6 section 5).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from primer.channel.correlation import CorrelationStore
from primer.int.storage_provider import StorageProvider
from primer.model.channel import Channel

if TYPE_CHECKING:
    from primer.channel.event_dispatch import ChannelRouteOutcome

logger = logging.getLogger(__name__)


class ChannelInboundRouter:
    """Resolve an inbound channel event to its destination + act."""

    def __init__(
        self,
        storage_provider: StorageProvider,
        correlation_store: CorrelationStore,
        event_bus=None,
        claim_engine=None,
        scheduler=None,
        workspace_registry=None,
        artifact_registry=None,
    ) -> None:
        self._sp = storage_provider
        self._correlation = correlation_store
        self._bus = event_bus
        self._claim_engine = claim_engine
        # S6 section 5: an inbound thread CREATES a session (needs the
        # workspace registry to allocate its on-disk slot) and STEERS it
        # (needs it again to append the instruction), so the inbound path
        # can no longer route with these unset.
        self._scheduler = scheduler
        self._workspace_registry = workspace_registry
        self._artifacts = artifact_registry

    async def route_event(
        self, *, event, channel: Channel, media_parts: list | None = None,
    ) -> "ChannelRouteOutcome":
        """Route a normalized :class:`ChannelEvent`. Side-effects plus outcome.

        Correlation-first (gate resume, then thread steer), else fire the
        channel triggers. There is no chat fallback any more: a platform
        thread IS a session (S6 section 5).
        """
        from primer.channel.event_dispatch import ChannelEventRouter
        from primer.observability import metrics
        from primer.trigger.subscribers import DispatchDeps

        event_type = getattr(event.type, "value", event.type)
        provider = getattr(event.provider, "value", event.provider)
        metrics.channel_events_normalized_total.labels(
            event_type=event_type, provider=provider,
        ).inc()

        router = ChannelEventRouter(
            storage_provider=self._sp,
            correlation_store=self._correlation,
            fire_deps=DispatchDeps(
                storage_provider=self._sp,
                claim_engine=self._claim_engine,
                scheduler=self._scheduler,
                workspace_registry=self._workspace_registry,
                event_bus=self._bus,
            ),
            event_bus=self._bus,
            artifact_registry=self._artifacts,
        )
        outcome = await router.route_event(
            event=event, channel=channel, media_parts=media_parts,
        )
        if outcome.kind != "ignored":
            metrics.channel_events_matched_total.labels(
                event_type=event_type, provider=provider,
            ).inc()
            metrics.channel_events_dispatched_total.labels(
                event_type=event_type, provider=provider,
            ).inc()
        return outcome


__all__ = ["ChannelInboundRouter"]
