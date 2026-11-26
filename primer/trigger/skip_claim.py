"""The cross-process skip-policy claim (review 2026-11-23 6.8 follow-up, X2.1).

A ``parallelism="skip"`` subscription's fire is a check-then-act (is a live
session in flight?) with no claim upstream on the webhook path: every POST
gets its own background task, so two processes delivering the same burst
each read "not busy" before either creates and both create. The claim makes
the check and the create atomic ACROSS processes:

* the row id is derived from the subscription id, so two claims conflict on
  the primary key on every backend (the ``claim_thread`` pattern);
* the winner holds the row from the busy check through the session create
  and releases it on every exit (``release_skip_claim`` in a ``finally``,
  guarded on the generation this acquire returned);
* a holder that crashes leaves the row until its TTL expires; the next claim
  takes a stale row over with ONE guarded write on the exact stale
  ``claimed_at`` (``seed.py``'s claim pattern), so two takers cannot both win.

The in-process asyncio lock (``primer/trigger/dispatch.py::_fire_lock``)
stays as the fast path, narrowed to skip-policy subscriptions; the claim is
the arbiter when the lock is not shared (another process).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from primer.model.except_ import ConflictError, NotFoundError
from primer.model.trigger_skip_claim import TriggerSkipClaim
from primer.storage import raw_generation

logger = logging.getLogger(__name__)

#: Crash backstop for a holder that dies without releasing (a kill -9 between
#: the claim and the release): a skipped fire is recoverable on the NEXT
#: fire, so the TTL only bounds how long a crashed process wedges its
#: subscription. Long enough that a legitimate dispatch (workspace
#: allocation, session create) never outruns it; short enough that a dead
#: replica un-wedges itself within a minute.
_SKIP_CLAIM_TTL = timedelta(seconds=60)


def _claim_id(subscription_id: str) -> str:
    """The deterministic row id: derived from the subscription id, so two
    claims of the same subscription conflict on the primary key on every
    backend (``claim_thread``'s pattern)."""
    digest = hashlib.sha256(subscription_id.encode()).hexdigest()[:32]
    return f"trigger-skip-claim-{digest}"


async def acquire_skip_claim(storage_provider, subscription_id: str):
    """Claim the skip gate for *subscription_id*.

    Returns the holder's GENERATION - the row's ``claimed_at`` as the store
    holds it (for :func:`release_skip_claim`'s guarded delete) - or ``None``
    when another fire owns the claim: either an unexpired holder (its fire is
    in flight and will release on exit) or a taker whose guarded write won
    before this one.
    """
    storage = storage_provider.get_storage(TriggerSkipClaim)
    claim_id = _claim_id(subscription_id)
    for _attempt in range(2):
        now = datetime.now(timezone.utc)
        try:
            created = await storage.create(TriggerSkipClaim(
                id=claim_id,
                subscription_id=subscription_id,
                claimed_at=now,
                expires_at=now + _SKIP_CLAIM_TTL,
            ))
            return raw_generation(created, "claimed_at")
        except ConflictError:
            pass
        row = await storage.get(claim_id)
        if row is None:
            continue  # the row vanished under us; the retry's insert wins
        if row.expires_at > now:
            return None  # a live holder
        # Expired: a crashed holder. Take the row over with ONE guarded write
        # on the exact stale value (primer/bootstrap/seed.py's claim pattern):
        # whoever's patch matches is the single winner; a guard mismatch or a
        # vanished row means another taker won.
        try:
            taken = await storage.patch_if(
                claim_id,
                {
                    "claimed_at": now.isoformat(),
                    "expires_at": (now + _SKIP_CLAIM_TTL).isoformat(),
                },
                where={"claimed_at": [raw_generation(row, "claimed_at")]},
            )
        except NotFoundError:
            return None  # deleted between the read and the write
        if taken is not None:
            return raw_generation(taken, "claimed_at")
    return None


async def release_skip_claim(storage_provider, subscription_id: str, generation) -> None:
    """Release the skip gate, only while the row still carries THIS fire's stamp.

    A holder whose dispatch outlived the TTL must not delete the claim a
    taker now holds (the guard, ``seed.py``'s ``_release_claim`` pattern).
    The TTL reaps a row whose release was lost, so a failure here only makes
    the subscription's next fire wait out the TTL - it cannot fire twice.
    """
    try:
        await storage_provider.get_storage(TriggerSkipClaim).delete_if(
            _claim_id(subscription_id),
            where={"claimed_at": [generation]},
        )
    except Exception:  # noqa: BLE001 - see the docstring
        logger.debug(
            "trigger skip claim release failed (the TTL reaps it)",
            exc_info=True,
        )


__all__ = [
    "SKIP_CLAIM_TTL_SECONDS",
    "acquire_skip_claim",
    "release_skip_claim",
]

SKIP_CLAIM_TTL_SECONDS = _SKIP_CLAIM_TTL.total_seconds()
