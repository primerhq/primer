"""WebhookSignature - one entry of the shared (cross-replica) webhook replay cache.

The public webhook endpoint's in-memory dict is the first-level replay check
(per process, cheap). This row is the cross-replica arbiter (review 2026-11-23
6.11 follow-up, X2.1): when an ACCEPTED timestamped signature is verified, the
endpoint inserts its row conditionally. The row id is derived from the
(trigger id, canonical expected signature hex) pair, so a second replica's
insert of the same captured request conflicts on the primary key on every
backend (as ``claim_thread``'s derived-id inserts do) and is refused
``replayed``.

``expires_at`` is the signature's own timestamp plus the 5-minute window -
the wall-clock moment the timestamp stops validating. An exact resubmission
only re-computes the same hex while its timestamp still validates, so an
unexpired row is always a live refusal; expired rows are pruned lazily on the
next accept (best effort).
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from primer.model.common import Identifiable


class WebhookSignature(Identifiable):
    """A recently accepted timestamped webhook signature."""

    _id_prefix: ClassVar[str] = "webhook-signature"

    trigger_id: str = Field(
        ...,
        description="The trigger the signature was accepted for.",
    )
    signature: str = Field(
        ...,
        description="The canonical expected hex (the server-computed MAC, no sha256= prefix).",
    )
    expires_at: datetime = Field(
        ...,
        description=(
            "Wall-clock moment the signature's own timestamp stops validating "
            "(int(ts) + the 5-minute window, UTC)."
        ),
    )


__all__ = ["WebhookSignature"]
