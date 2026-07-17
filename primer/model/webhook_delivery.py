"""WebhookDelivery model - durable record of an inbound webhook fire.

A row is written by the public webhook endpoint (``POST /v1/webhooks/{token}``)
BEFORE it returns 202 and BEFORE the in-process ``BackgroundTask`` dispatches
the trigger. Without this durable marker a crash between the 202 and dispatch
completion permanently loses the delivery (senders do not retry a 202).

The row ``id`` is the endpoint's ``fire_id`` (``fire-{trigger_id}-{ms}``) plus a
random per-request suffix, so every inbound request gets its own row. The bare
fire_id is NOT usable as the id: it keys on (trigger, arrival millisecond) and
correlates with neither the sender nor the payload, so two distinct events for
one trigger in the same millisecond collided on the primary key and the second
was dropped.

Delivery is at-least-once and NOT idempotent for duplicate POSTs: a sender
retrying seconds later gets a different fire_id and fires the trigger again.
Deduping genuine duplicates would require a sender-supplied idempotency key,
which is out of scope.

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

    ``id`` is endpoint-computed (``fire_id`` + a random per-request suffix)
    and never auto-generated, so the model carries no ``_id_prefix`` -
    callers always supply the id.
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
        description="pending -> done/failed. Recovery re-fires stale 'pending'.",
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
        description=(
            "Dispatch attempts STARTED for this delivery. Written by whoever "
            "starts one: the endpoint creates the row with 1 (its "
            "BackgroundTask) and startup recovery bumps it BEFORE each "
            "re-dispatch, so an attempt that crashes the process is still "
            "counted. Recovery abandons the row (marks it 'failed') once it "
            "reaches the attempt cap, which stops a poison-pill delivery from "
            "re-firing on every boot forever."
        ),
    )


__all__ = ["WebhookDelivery", "WebhookDeliveryStatus"]
