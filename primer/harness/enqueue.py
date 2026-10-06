"""Making an operation enqueued on a harness row claimable.

An operation is enqueued by writing ``Harness.pending_operation``. The worker claims harness work only through lease rows: the
claim query joins ``leases`` to the harness table, and the harness adapter's eligibility is ``pending_operation IS NOT NULL``
(:class:`primer.claim.adapters.harnesses.HarnessClaimAdapter`). A surface that writes the row and stops there enqueues an
operation nothing will run, and every later operation on that harness answers a conflict. This is the one definition of the
step after the row write, shared by the REST routes (:mod:`primer.api.routers.harness`) and the ``harness`` toolset
(:mod:`primer.toolset.harness`), so the two cannot drift apart again.
"""

from __future__ import annotations

from typing import Any

from primer.int.claim import CLAIM_PRIORITY_OPERATOR, ClaimKind


async def announce_enqueued(*, harness_id: str, event_bus: Any, claim_engine: Any) -> None:
    """Publish ``harness-claimable`` and upsert the harness's lease at operator priority.

    Call it AFTER the row's ``pending_operation`` was written. Both collaborators are optional (API-only mode has no bus,
    a standalone build no engine): a missing one is skipped, the row write alone having already happened.
    """
    if event_bus is not None:
        await event_bus.publish("harness-claimable", {"harness_id": harness_id})
    if claim_engine is not None:
        await claim_engine.upsert(ClaimKind.HARNESS, harness_id, priority=CLAIM_PRIORITY_OPERATOR)


__all__ = ["announce_enqueued"]
