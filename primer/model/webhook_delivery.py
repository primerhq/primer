"""WebhookDelivery model - durable record of an inbound webhook fire.

A row is written by the public webhook endpoint (``POST /v1/webhooks/{token}``)
BEFORE it returns 202 and BEFORE the in-process ``BackgroundTask`` dispatches
the trigger. Without this durable marker a crash between the 202 and dispatch
completion permanently loses the delivery (senders do not retry a 202).

The row ``id`` is the ``fire_id`` (``fire-{trigger_id}-{ms}``) computed by the
endpoint, so a duplicate inbound request for the same logical instant collides
on the primary key and is naturally idempotent at the storage layer.

Lifecycle:
- ``pending``  - created; dispatch not yet confirmed complete.
- ``done``     - the background dispatch finished (best-effort mark).
- ``failed``   - the background dispatch raised (best-effort mark).

Startup recovery re-dispatches ``pending`` rows older than a small grace
window (their owning process died before marking them), giving inbound
webhooks at-least-once delivery instead of fire-and-forget.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from primer.model.common import Identifiable


WebhookDeliveryStatus = Literal["pending", "done", "failed"]


class WebhookDelivery(Identifiable):
    """Persisted record of one inbound webhook fire.

    ``id`` is the endpoint-computed ``fire_id`` (never auto-generated), so
    the model carries no ``_id_prefix`` - callers always supply the id.
    """

    trigger_id: str = Field(
        ...,
        min_length=1,
        description="Trigger whose subscriptions this delivery dispatches.",
    )
    extra_context: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Snapshot of the fire_context extras the endpoint captured "
            "(webhook_body / webhook_headers / webhook_query / "
            "webhook_method), replayed verbatim on re-dispatch."
        ),
    )
    status: WebhookDeliveryStatus = Field(
        default="pending",
        description="pending → done/failed. Recovery re-fires stale 'pending'.",
    )
    created_at: datetime = Field(
        ...,
        description="When the endpoint accepted the webhook (the fire instant).",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="When the dispatch was marked done/failed; None while pending.",
    )
    attempts: int = Field(
        default=0,
        ge=0,
        description="Number of dispatch attempts (incremented per dispatch).",
    )


__all__ = ["WebhookDelivery", "WebhookDeliveryStatus"]
