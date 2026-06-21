"""Channel trigger source.

Channel triggers are the event-source anchor that channel subscriptions
attach to. They fire when a normalized ``ChannelEvent`` arrives from an
inbound channel provider. Like the webhook source, they are NOT
claim-engine-driven (no next_fire_at; eligible_for_claim=False).

The fire_context they produce carries the firing ``ChannelEvent`` (as a
JSON-mode dict) under ``fire_context["event"]`` so matcher evaluation and
payload templates can reference it.
"""

from __future__ import annotations

from datetime import datetime

from primer.model.trigger import Trigger


class ChannelSource:
    """Source for kind='channel' triggers.

    Channel triggers do not use the claim machine. Fires arrive from the
    inbound channel router and are dispatched immediately.
    """

    kind = "channel"
    eligible_for_claim = False

    def compute_next_fire_at(
        self,
        trigger: Trigger,
        *,
        now: datetime,
    ) -> datetime | None:
        # Channel triggers never have a scheduled next_fire_at.
        return None

    def build_fire_context(
        self,
        trigger: Trigger,
        *,
        fired_at: datetime,
        scheduled_for: datetime | None = None,
        # The firing ChannelEvent injected by the inbound router.
        event=None,
    ) -> dict:
        ctx: dict = {
            "trigger_id": trigger.id,
            "trigger_slug": trigger.slug,
            "kind": "channel",
            "fired_at": fired_at.isoformat(),
            "scheduled_for": None,
        }
        if event is not None:
            ctx["event"] = (
                event.model_dump(mode="json")
                if hasattr(event, "model_dump")
                else event
            )
        return ctx


__all__ = ["ChannelSource"]
