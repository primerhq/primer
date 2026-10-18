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
from primer.model.workspace_session import PeerAttribution, WorkspaceSession
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.pending_messages import PendingQueueFull, store_pending_steer
from primer.session.steer_routing import ROUTE_PENDING, route_steer

logger = logging.getLogger(__name__)

DELIVERED_WOKEN = "woken"
DELIVERED_QUEUED = "queued"
DELIVERED_SKIPPED_BUSY = "skipped_busy"
DELIVERED_MISSING = "missing"
DELIVERED_QUEUE_FULL = "queue_full"


@dataclass
class SteerDelivery:
    """What happened to one delivery attempt.

    ``detail`` and ``conflict`` say why a ``DELIVERED_MISSING`` attempt found the session unable to take the message: the text of the NotFoundError
    or ConflictError :func:`wake_session` raised (the workspace was removed under the write, the session cannot be reopened), and whether it was the
    conflict. Both stay at their defaults for a session that was absent from the start, and for every other outcome.
    """

    outcome: str
    session_id: str | None = None
    detail: str | None = None
    conflict: bool = False


async def deliver_steer(
    *,
    session_id: str,
    text: str,
    parallelism: str,
    human_intent: bool,
    storage_provider: Any,
    scheduler: Any,
    claim_engine: Any,
    workspace_registry: Any,
    event_bus: Any = None,
    peer: PeerAttribution | None = None,
) -> SteerDelivery:
    """Steer ``session_id`` with ``text``, honouring the parallelism mode.

    ``parallelism="skip"`` drops the steer when the target already has a
    non-terminal turn; ``"queue"`` stores it as a pending row the drain
    checkpoint realizes. An idle target is woken immediately.

    ``peer`` (pivot P3) says the text was sent by another session
    (``message_session``). It goes to :func:`wake_session` when the target
    is woken, and into the pending row's ``attribution`` when the message
    is queued, so the sender and the hop count reach the USER_INPUT record
    whichever way the message is delivered.

    The outcome is ``DELIVERED_QUEUED`` when the message was stored and not
    written: the target had a turn open, or :func:`wake_session` held the
    message because the session is paused (it returns the row untouched,
    ``turn_status`` not claimable). A wake that lands arms the turn
    (``claimable``) and reports ``DELIVERED_WOKEN``; so does a wake that
    returns nothing, which is what the test doubles do.

    An ENDED target is never queued, whatever its park columns say
    (:func:`~primer.session.steer_routing.route_steer`): it is woken, which
    reopens it, or it cannot be reopened and the outcome is
    ``DELIVERED_MISSING`` with nothing stored.

    ``human_intent`` (01a08c08, required, no default): forwarded verbatim
    to :func:`wake_session` -- True for a channel thread reply, False for
    a trigger's session_append subscription. Both callers "mean the same
    thing" per this module's docstring for ROUTING, but they are not the
    same thing for a PAUSED target: a channel reply is a person; a
    trigger fire is not, and must not silently clear an operator's pause.

    A row that is absent OR cannot take a message (a non-restartable ENDED
    session, or a CANCELLED one when the message is a peer's: an operator's
    Cancel sticks) reports ``DELIVERED_MISSING``, so the channel router
    remaps the thread to a fresh session instead of dropping the message.

    A peer message (``peer`` given) to a session whose queue already holds
    the cap's worth of messages reports ``DELIVERED_QUEUE_FULL`` with nothing
    stored: it never takes room from a person (see
    :func:`~primer.session.pending_messages.store_pending_steer`). That holds
    both ways a message is queued here, behind a turn or a park (stored by
    this function) and behind a pause (held by :func:`wake_session`). Only a
    peer message can get this outcome; every other caller takes the room.
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
        try:
            await store_pending_steer(
                storage_provider=storage_provider,
                session=row,
                text=text,
                workspace_registry=workspace_registry,
                event_bus=event_bus,
                attribution=peer.as_attribution() if peer is not None else None,
            )
        except PendingQueueFull:
            return SteerDelivery(outcome=DELIVERED_QUEUE_FULL, session_id=session_id)
        return SteerDelivery(outcome=DELIVERED_QUEUED, session_id=session_id)
    try:
        woken = await wake_session(
            workspace_id=row.workspace_id,
            session_id=session_id,
            instruction=text,
            human_intent=human_intent,
            deps=SessionWakeDeps(
                storage_provider=storage_provider,
                scheduler=scheduler,
                claim_engine=claim_engine,
                workspace_registry=workspace_registry,
                event_bus=event_bus,
            ),
            peer=peer,
        )
    except PendingQueueFull:
        # The pause hold stored nothing: the queue it would have joined is full and the message is a peer's.
        return SteerDelivery(outcome=DELIVERED_QUEUE_FULL, session_id=session_id)
    except (NotFoundError, ConflictError) as exc:
        # Raced deletion, or an ENDED session whose ended_reason forbids a
        # restart (or, for a peer message, a session an operator cancelled).
        # Either way this target cannot receive the message: report
        # it missing so the caller can create a fresh one. The reason travels
        # with it, for a caller that tells its own caller why.
        logger.info(
            "steer delivery: session %s cannot accept a message", session_id,
            exc_info=True,
        )
        return SteerDelivery(
            outcome=DELIVERED_MISSING,
            detail=getattr(exc, "message", None) or str(exc),
            conflict=isinstance(exc, ConflictError),
        )
    if woken is not None and woken.turn_status != "claimable":
        return SteerDelivery(outcome=DELIVERED_QUEUED, session_id=session_id)
    return SteerDelivery(outcome=DELIVERED_WOKEN, session_id=session_id)


__all__ = [
    "DELIVERED_MISSING",
    "DELIVERED_QUEUED",
    "DELIVERED_QUEUE_FULL",
    "DELIVERED_SKIPPED_BUSY",
    "DELIVERED_WOKEN",
    "SteerDelivery",
    "deliver_steer",
]
