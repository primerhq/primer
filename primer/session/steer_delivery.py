"""One steer-delivery path shared by every machine entrypoint.

A trigger's ``session_append`` subscription and a channel thread reply both
mean the same thing: put this text into that session. Both go through
:func:`deliver_steer` so the S1 routing rule (a steer that lands while a
turn is open becomes a ``PendingSessionMessage``, never a second
USER_INPUT) is applied in exactly one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import WorkspaceSession
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.pending_messages import store_pending_steer
from primer.session.steer_routing import ROUTE_PENDING, route_steer

logger = logging.getLogger(__name__)

DELIVERED_WOKEN = "woken"
DELIVERED_QUEUED = "queued"
DELIVERED_SKIPPED_BUSY = "skipped_busy"
DELIVERED_MISSING = "missing"


@dataclass
class SteerDelivery:
    """What happened to one delivery attempt."""

    outcome: str
    session_id: str | None = None


async def deliver_steer(
    *,
    session_id: str,
    text: str,
    parallelism: str,
    storage_provider: Any,
    scheduler: Any,
    claim_engine: Any,
    workspace_registry: Any,
    event_bus: Any = None,
) -> SteerDelivery:
    """Steer ``session_id`` with ``text``, honouring the parallelism mode.

    ``parallelism="skip"`` drops the steer when the target already has a
    non-terminal turn; ``"queue"`` stores it as a pending row the drain
    checkpoint realizes. An idle target is woken immediately.

    A row that is absent OR cannot take a message (a non-restartable ENDED
    session) reports ``DELIVERED_MISSING``, so the channel router remaps the
    thread to a fresh session instead of dropping the message.
    """
    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    if row is None:
        return SteerDelivery(outcome=DELIVERED_MISSING)
    if route_steer(row) == ROUTE_PENDING:
        if parallelism == "skip":
            return SteerDelivery(
                outcome=DELIVERED_SKIPPED_BUSY, session_id=session_id,
            )
        await store_pending_steer(
            storage_provider=storage_provider,
            session_id=session_id,
            text=text,
        )
        return SteerDelivery(outcome=DELIVERED_QUEUED, session_id=session_id)
    try:
        await wake_session(
            workspace_id=row.workspace_id,
            session_id=session_id,
            instruction=text,
            deps=SessionWakeDeps(
                storage_provider=storage_provider,
                scheduler=scheduler,
                claim_engine=claim_engine,
                workspace_registry=workspace_registry,
                event_bus=event_bus,
            ),
        )
    except (NotFoundError, ConflictError):
        # Raced deletion, or an ENDED session whose ended_reason forbids a
        # restart. Either way this target cannot receive the message: report
        # it missing so the caller can create a fresh one.
        logger.info(
            "steer delivery: session %s cannot accept a message", session_id,
            exc_info=True,
        )
        return SteerDelivery(outcome=DELIVERED_MISSING)
    return SteerDelivery(outcome=DELIVERED_WOKEN, session_id=session_id)


__all__ = [
    "DELIVERED_MISSING",
    "DELIVERED_QUEUED",
    "DELIVERED_SKIPPED_BUSY",
    "DELIVERED_WOKEN",
    "SteerDelivery",
    "deliver_steer",
]
