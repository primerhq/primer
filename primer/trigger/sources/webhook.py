"""Webhook trigger source.

Webhook triggers are event-driven: they fire when an HTTP POST arrives
at the public ``/v1/webhooks/{token}`` endpoint. They are NOT
claim-engine-driven (no next_fire_at; eligible_for_claim=False).

The fire_context they produce carries the webhook payload fields so
downstream dispatchers and payload templates can reference them.
"""

from __future__ import annotations

from datetime import datetime

from primer.model.trigger import Trigger


class WebhookSource:
    """Source for kind='webhook' triggers.

    Webhook triggers do not use the claim machine. Fires arrive from the
    public inbound endpoint and are dispatched immediately.
    """

    kind = "webhook"
    eligible_for_claim = False

    def compute_next_fire_at(
        self,
        trigger: Trigger,
        *,
        now: datetime,
    ) -> datetime | None:
        # Webhook triggers never have a scheduled next_fire_at.
        return None

    def build_fire_context(
        self,
        trigger: Trigger,
        *,
        fired_at: datetime,
        scheduled_for: datetime | None = None,
        # Webhook-specific extras injected by the inbound endpoint.
        webhook_body: str | None = None,
        webhook_headers: dict | None = None,
        webhook_query: dict | None = None,
        webhook_method: str = "POST",
    ) -> dict:
        ctx: dict = {
            "trigger_id": trigger.id,
            "trigger_slug": trigger.slug,
            "kind": "webhook",
            "fired_at": fired_at.isoformat(),
            "scheduled_for": None,
        }
        if webhook_body is not None:
            ctx["webhook_body"] = webhook_body
        if webhook_headers is not None:
            ctx["webhook_headers"] = webhook_headers
        if webhook_query is not None:
            ctx["webhook_query"] = webhook_query
        ctx["webhook_method"] = webhook_method
        return ctx


__all__ = ["WebhookSource"]
