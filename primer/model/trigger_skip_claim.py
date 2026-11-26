"""TriggerSkipClaim - the cross-process skip-gate claim.

A ``parallelism="skip"`` subscription's fire is a check-then-act (is a live
session in flight for this subscription?), historically serialized by a
per-process asyncio lock alone. Two API replicas delivering the same webhook
burst each read "not busy" and each created a session (review 2026-11-23
6.8, cross-process follow-up, X2.1).

The claim closes that across processes: the winner's conditional insert (the
row id is derived from the subscription id, so two claims conflict on the
primary key on every backend, as ``claim_thread``'s do) is held from the
busy check through the session create and deleted on every exit
(``primer/trigger/skip_claim.py``). A holder that crashes leaves the row
until its TTL expires; the next claim takes a stale row over.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from primer.model.common import Identifiable


class TriggerSkipClaim(Identifiable):
    """One in-flight skip-policy fire of a subscription."""

    _id_prefix: ClassVar[str] = "trigger-skip-claim"

    subscription_id: str = Field(
        ...,
        description="The subscription whose skip-policy fire holds this claim.",
    )
    claimed_at: datetime = Field(
        ...,
        description="When the winning fire took the claim (UTC).",
    )
    expires_at: datetime = Field(
        ...,
        description=(
            "Crash backstop: a holder that dies without releasing leaves the "
            "row until this instant, after which the next claim takes it over."
        ),
    )


__all__ = ["TriggerSkipClaim"]
