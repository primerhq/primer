"""Deferred-steer queue for sessions (port of the chat pending drain).

The routing rule: a steer arriving while the session already has a
non-terminal turn is stored here, with NO USER_INPUT record written, and
realized at the drain checkpoint instead. Allocating a seq at receipt is
what collided with the in-flight turn's assistant_token seqs on the chat
surface (primer/model/chats.py:280-308), so these rows carry none.

Realization goes through :func:`wake_session` rather than writing the
record directly, so the one canonical persist-and-wake path stays
canonical: USER_INPUT record, title derivation, claimable flip, and the
scheduler pulse all keep happening in exactly one place.

Exactly ONE row is realized per checkpoint. Draining the whole queue at
once would write several user messages against a single turn and break
the 1:1 user_input-to-terminal pairing the drain counts on
(primer/session/turns.py).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.workspace_session import (
    CANVAS_EVENT_PAYLOAD_KEY,
    MACHINE_ATTRIBUTION_KEY,
    PEER_PAYLOAD_KEY,
    PeerAttribution,
    PendingSessionMessage,
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.enqueue import CanvasClickDoesNotReopen, OnlyAPersonReopens, SessionWakeReceipt, wake_session
from primer.session.persistence import WorkspaceMessageWriter
from primer.session.seq_reservation import reserve_next_seq

logger = logging.getLogger(__name__)

# 01a08c08: a module-level constant, not a config subsystem on purpose (per
# the ruling: keep it small). An automated source (a trigger firing on a
# schedule, an agent steering another session) can queue indefinitely
# against a session paused for days -- nothing else in this codebase drains
# the queue until a human resumes it, so an unbounded queue trades "the
# pause gets silently cleared" for "the operator drains a stale backlog one
# turn at a time before their own message gets a turn," which is worse.
_MAX_PENDING_PER_SESSION = 50

# Why a message was dropped, as the PAUSE_SUPERSEDED record says it (the console shows "queued message dropped (<reason>): <text>").
_REASON_CAP = f"pending queue exceeded {_MAX_PENDING_PER_SESSION}"
_REASON_CANCELLED = "the session was cancelled, and a message no person wrote does not reopen it"
_REASON_CANVAS_ENDED = "the session has ended, and a canvas click does not reopen it"

_OLDEST_FIRST = [
    OrderBy(field="enqueued_at", direction="asc"),
    OrderBy(field="id", direction="asc"),
]


class PendingQueueFull(ConflictError):
    """A message from another session was refused: the session already has ``_MAX_PENDING_PER_SESSION`` messages waiting.

    The queue's cap drops the oldest message to make room, and a message from another session (``message_session``, pivot P3) must never do that to a
    person's steer: three senders at the rate limit can fill a busy coordinator's queue. So such a message is refused instead, before anything is
    written, and its sender is told the session is busy. A person's steer, a trigger and a channel reply are not refused: they take the room.
    """


def _carries_peer(attribution: dict | None) -> bool:
    """Whether a queued message's attribution says it came from another session.

    Reads the key and not the value, as :meth:`PeerAttribution.from_attribution` does to tell "no peer" from "a malformed peer": the cap must be able to
    tell a message from another session without validating it.
    """
    return isinstance(attribution, dict) and PEER_PAYLOAD_KEY in attribution


def _written_by_a_person(attribution: dict | None) -> bool:
    """Whether a queued message was written by a person: it carries neither a peer's attribution nor the machine marker
    (:func:`primer.session.enqueue.queued_attribution`). A row queued before the marker existed carries neither and is a person's, as it was read then."""
    if not isinstance(attribution, dict):
        return True
    return PEER_PAYLOAD_KEY not in attribution and MACHINE_ATTRIBUTION_KEY not in attribution


def _canvas_payload(attribution: dict | None) -> dict | None:
    """``{"canvas_event": ...}`` for a queued canvas click (pivot P5), to go back into the wake that realizes it, else ``None``.

    The event is the one the delivery queued (``primer.canvas.delivery.event_attribution``, without what the user entered): the USER_INPUT
    record names it, the wake leaves it out of the digest and marks it read once its block is written. Anything else under the key is
    ignored, and the message is realized as a plain one.
    """
    found = attribution.get(CANVAS_EVENT_PAYLOAD_KEY) if isinstance(attribution, dict) else None
    return {CANVAS_EVENT_PAYLOAD_KEY: found} if isinstance(found, dict) else None


def _dropped_text(row: PendingSessionMessage) -> str:
    """What the record of a dropped queued message says it was: its text, or, for a queued canvas click (pivot P5), a summary of the event
    (``canvas event e1 (emit on viz)``), never its ``<canvas_event>`` block, whose text holds what the user entered (the lead's ruling 3 on
    #892 round 2: the record is a structured copy)."""
    canvas = _canvas_payload(row.attribution)
    if canvas is None:
        return _text_of(row)
    event = canvas[CANVAS_EVENT_PAYLOAD_KEY]
    return f"canvas event {event.get('id')} ({event.get('kind')} on {event.get('element')})"


def _queued_for(session_id: str) -> Predicate:
    return Predicate(left=FieldRef(name="session_id"), op=Op.EQ, right=Value(value=session_id))


def _text_of(row: PendingSessionMessage) -> str:
    return "\n".join(
        p.get("text", "") for p in row.parts
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
    )


async def has_queued_messages(storage_provider: Any, session_id: str) -> bool:
    """Whether ``session_id`` has a message waiting in its pending queue: a new message then goes behind it (``route_steer``'s ``queued``).

    One read of at most one row. A storage that cannot be read answers False (logged): the conditional arm in ``wake_session`` still keeps two
    wakes from arming one turn, so the worst a failed read costs is the order of two messages, never a second USER_INPUT for a turn.
    """
    try:
        page = await storage_provider.get_storage(PendingSessionMessage).find(
            _queued_for(session_id), OffsetPage(offset=0, length=1), order_by=_OLDEST_FIRST,
        )
    except Exception:  # noqa: BLE001 - see above: ordering only, never a second turn
        logger.warning("pending_messages: could not read the queue of session %s; routing as if it were empty", session_id, exc_info=True)
        return False
    return bool(page.items)


async def _queue_is_full(storage: Any, session_id: str) -> bool:
    """Whether ``session_id`` already has ``_MAX_PENDING_PER_SESSION`` messages waiting: the row at that position exists."""
    page = await storage.find(
        _queued_for(session_id), OffsetPage(offset=_MAX_PENDING_PER_SESSION - 1, length=1), order_by=_OLDEST_FIRST,
    )
    return bool(page.items)


async def store_pending_steer(
    *,
    storage_provider: Any,
    session: WorkspaceSession,
    text: str,
    workspace_registry: Any,
    event_bus: Any | None = None,
    attribution: dict | None = None,
    client_msg_id: str | None = None,
) -> PendingSessionMessage:
    """Queue a follow-up steer without touching the message log.

    ``session`` (not just ``session_id``) names the session a capacity drop
    (see ``_enforce_pending_cap`` below) is announced for. The announcement's
    seq is RESERVED on the stored row (``reserve_next_seq``: it reads the
    row fresh and takes the next seq, so the snapshot passed in may be stale
    and the row's ``last_seq`` moves), not taken from ``session.last_seq``.

    A message from another session (``attribution`` carries a ``peer``) never
    takes room from a person: when the session already has the cap's worth of
    messages waiting it raises :class:`PendingQueueFull` before anything is
    written. Two such messages can still pass that check together (the check
    and the store are not one step), and then the cap drops the oldest PEER
    message first (``_enforce_pending_cap``), so a person's steer is never the
    one that goes.
    """
    storage = storage_provider.get_storage(PendingSessionMessage)
    if _carries_peer(attribution) and await _queue_is_full(storage, session.id):
        raise PendingQueueFull(
            f"Session {session.id!r} already has {_MAX_PENDING_PER_SESSION} messages waiting; a message from another session does not take their place"
        )
    now = datetime.now(UTC)
    row = PendingSessionMessage(
        id=f"{session.id}:pending:{now.isoformat()}:{uuid.uuid4().hex[:8]}",
        session_id=session.id,
        parts=[{"type": "text", "text": text}],
        attribution=attribution,
        client_msg_id=client_msg_id,
        enqueued_at=now,
        created_at=now,
    )
    await storage.create(row)
    await _enforce_pending_cap(
        storage_provider=storage_provider,
        session=session,
        workspace_registry=workspace_registry,
        event_bus=event_bus,
    )
    return row


async def _enforce_pending_cap(
    *,
    storage_provider: Any,
    session: WorkspaceSession,
    workspace_registry: Any,
    event_bus: Any | None,
) -> None:
    """Drop queued messages once the session exceeds the cap: the oldest message from another session first, then the oldest of the rest.

    The drop must never be silent -- that would reintroduce the exact
    defect this task exists to remove, one layer down. Every drop writes
    a PAUSE_SUPERSEDED(action="dropped") record naming the dropped text,
    so an operator asking "did that ever arrive" can find the answer.

    A message from another session goes before anything else (pivot P3, lead ruling R1): the queue is a person's place to leave a steer, and a peer
    must not be able to fill it with traffic that pushes the steer out. A person's steer that takes the queue past the cap therefore drops the oldest
    peer message; only when there is none does it drop the oldest row, as it always did.
    """
    storage = storage_provider.get_storage(PendingSessionMessage)
    page = await storage.find(
        _queued_for(session.id),
        # Fetches a generous ceiling, not just cap+1: this runs after
        # every single store_pending_steer call, so the excess is
        # normally exactly one row -- but fetching more stays correct
        # even if something else ever inserts a burst directly (a lowered
        # cap in a future change, a bug elsewhere), trimming it back down
        # to the cap in one pass instead of silently leaving the rest.
        # OffsetPage.length is itself capped at 200 storage-wide, so this
        # can't just keep multiplying the cap for "more margin".
        OffsetPage(offset=0, length=min(_MAX_PENDING_PER_SESSION * 3, 200)),
        order_by=_OLDEST_FIRST,
    )
    rows = list(page.items)
    excess = len(rows) - _MAX_PENDING_PER_SESSION
    if excess <= 0:
        return
    victims = [row for row in rows if _carries_peer(row.attribution)][:excess]
    if len(victims) < excess:
        # Fewer messages from other sessions than rows over the cap: they all go, and the oldest of the rest make up the difference.
        gone = {row.id for row in victims}
        victims += [row for row in rows if row.id not in gone][: excess - len(victims)]
    for row in victims:
        await storage.delete(row.id)
        await _record_dropped_pending(
            workspace_registry=workspace_registry,
            event_bus=event_bus,
            storage_provider=storage_provider,
            workspace_id=session.workspace_id,
            session_id=session.id,
            dropped_text=_dropped_text(row),
            enqueued_at=row.enqueued_at,
            reason=_REASON_CAP,
        )


async def _record_dropped_pending(
    *,
    workspace_registry: Any,
    event_bus: Any | None,
    storage_provider: Any,
    workspace_id: str,
    session_id: str,
    dropped_text: str,
    enqueued_at: datetime,
    reason: str,
) -> None:
    """Best-effort announcement of a dropped pending message: what it said, when it was queued, and why it was dropped.

    Advisory, like every other tick publish in this module's family: a
    write failure here must not block the steer that triggered it, but it
    IS logged (unlike a plain advisory) because a swallowed exception here
    would defeat the entire point of this function.

    The record's seq is RESERVED on the row before it is written (the row's ``last_seq`` has to move so the next writer does not repeat the seq:
    ticket 01a11cd8).
    """
    if workspace_registry is None:
        return
    try:
        ws = await workspace_registry.get_workspace(workspace_id)
        if ws is None:
            return
        reserved = await reserve_next_seq(storage_provider.get_storage(WorkspaceSession), session_id)
        if reserved is None:
            logger.warning("pending_messages: no seq could be reserved to record a dropped pending message for session %s", session_id)
            return
        writer = WorkspaceMessageWriter(
            workspace_io=ws, session_id=session_id, start_seq=reserved - 1,
        )
        seq = await writer.append(SessionMessageRecord(
            seq=1,  # overwritten by the writer's monotonic counter
            kind=SessionMessageKind.PAUSE_SUPERSEDED,
            payload={
                "action": "dropped",
                "text": dropped_text,
                "enqueued_at": enqueued_at.isoformat(),
                "reason": reason,
            },
            created_at=datetime.now(UTC),
        ))
        await writer.flush()
    except Exception:  # noqa: BLE001 -- advisory, never block the steer
        logger.exception(
            "pending_messages: failed to record a dropped pending message "
            "for session %s", session_id,
        )
        return
    if event_bus is not None:
        try:
            await event_bus.publish(f"session:{session_id}:tick", {"seq": seq})
        except Exception:  # noqa: BLE001 -- advisory
            logger.exception(
                "pending_messages: failed to publish dropped-pending tick "
                "for session %s", session_id,
            )


async def realize_next_pending(
    *,
    storage_provider: Any,
    workspace_id: str,
    session_id: str,
    wake_deps: Any,
) -> bool:
    """Realize the oldest queued steer into a real turn.

    Returns True when a row was realized and the session woken. The realize
    takes the same conditional path as every routed wake (audit G7-03 (b)):
    ``wake_session`` is handed the row's id (``queued_message_id``) and a
    receipt, and arms the turn with ONE conditional write of the session row
    (idle -> claimable) before it writes the USER_INPUT. A session that is
    busy again (another wake armed it since the turn ended), paused, or whose
    message another drain already delivered, keeps the row where it is, at
    the head of the queue, and this returns False: nothing is written and the
    next turn boundary (or the queue sweep) realizes it.

    The row is deleted only AFTER its wake landed (audit G7-14): a crash, a
    workspace that refuses the write, or any other failure between the read
    and the wake leaves the message queued instead of losing it. The cost is
    the other side of that trade: a crash after the USER_INPUT is written and
    before the delete delivers the message a second time, at the next
    realize.

    A message that came from another session carries its sender and hop
    count in ``attribution`` (pivot P3); they go back into the wake, so the
    USER_INPUT record names the sender and the session's ``peer_hops``
    becomes the message's hop count. Dropping them here would restart every
    reply chain at hop 1 whenever a reply was queued behind a turn. A
    malformed attribution is logged and the message is realized as a plain
    one: it must not be lost, and it must not wedge the queue.

    A canvas click that was queued (pivot P5) carries its event the same
    way (``attribution["canvas_event"]``); it goes back into the wake as
    ``extra_payload``, so the USER_INPUT record names the event, the digest
    leaves it out and the wake marks it read.

    A message the session then refuses is dropped, on the record, and the drain goes on to the next row (lead rulings R4 and R1): the session was
    cancelled and no person wrote the message (another session sent it, or a trigger fire or a tool call queued it: the row's attribution says so),
    and only a person's message reopens a cancelled session (:class:`~primer.session.reset.OnlyAPersonReopens`; ruling 1 of the G7 fixes extends R4
    from peers to every message no person wrote). The wake is still a replay (``human_intent=False``: it does not clear a pause that landed since),
    but a person's steer is woken as written by a person (``by_a_person``), so it reopens a cancelled session as the lead's ruling on ticket 01a1120e
    has it. The drop is announced like a cap drop, and it does not use up the checkpoint: the person's steer queued behind it is still realized now,
    because no later checkpoint will come for a session that stays ended. Nothing else is skipped: the rows behind a realized one wait for the next
    checkpoint, as ever. (A pause that landed since no longer stores the message again: it stays in its place, so the hold can no longer find the
    queue full and drop it.)
    """
    while True:
        realized = await _realize_oldest(
            storage_provider=storage_provider, workspace_id=workspace_id, session_id=session_id, wake_deps=wake_deps,
        )
        if realized is not None:
            return realized


async def _realize_oldest(
    *,
    storage_provider: Any,
    workspace_id: str,
    session_id: str,
    wake_deps: Any,
) -> bool | None:
    """One pass of :func:`realize_next_pending`: the oldest row, woken, left or dropped. ``None`` is a row the session refused and this dropped:
    look at the next."""
    storage = storage_provider.get_storage(PendingSessionMessage)
    page = await storage.find(_queued_for(session_id), OffsetPage(offset=0, length=1), order_by=_OLDEST_FIRST)
    rows = list(page.items)
    if not rows:
        return False
    row = rows[0]
    text = _text_of(row)
    if not text:
        # Reaped rather than left at the head of the queue, where it
        # would block every later follow-up behind an empty wake.
        await _delete_queued(storage, row.id)
        return False
    peer = None
    try:
        peer = PeerAttribution.from_attribution(row.attribution)
    except ValidationError:
        logger.warning("realize: ignoring a malformed peer attribution on pending row %s", row.id)
    receipt = SessionWakeReceipt()
    try:
        await wake_session(
            workspace_id=workspace_id,
            session_id=session_id,
            instruction=text,
            # 01a08c08: this is a REPLAY of an earlier human message that
            # arrived while the session was busy, released now by the drain
            # checkpoint -- not fresh intent at this moment. If the session
            # has since been paused, this wake must queue behind that pause
            # like any other non-human wake, not resume it.
            human_intent=False,
            # Who WROTE it, which the queue kept: a person's steer still reopens a session cancelled since; a peer's or a machine's does not.
            by_a_person=_written_by_a_person(row.attribution),
            peer=peer,
            # A queued canvas click (pivot P5) names its event on the record, and is marked read by this wake.
            extra_payload=_canvas_payload(row.attribution),
            deps=wake_deps,
            queued_message_id=row.id,
            receipt=receipt,
        )
    except (OnlyAPersonReopens, CanvasClickDoesNotReopen) as refusal:
        logger.info("realize: dropped a queued message on %s (%s)", session_id, type(refusal).__name__)
        await _delete_queued(storage, row.id)
        await _record_dropped_pending(
            workspace_registry=wake_deps.workspace_registry,
            event_bus=wake_deps.event_bus,
            storage_provider=storage_provider,
            workspace_id=workspace_id,
            session_id=session_id,
            dropped_text=_dropped_text(row),
            enqueued_at=row.enqueued_at,
            reason=_REASON_CANVAS_ENDED if isinstance(refusal, CanvasClickDoesNotReopen) else _REASON_CANCELLED,
        )
        return None
    if receipt.outcome != "woken":
        # Busy again, paused, or already delivered by another drain: the message stays where it is (see realize_next_pending).
        logger.info("realize: left the queued message %s of session %s in place (%s)", row.id, session_id, receipt.outcome)
        return False
    # Only now that the wake landed (its USER_INPUT is written and the turn armed) does the message leave the queue (audit G7-14).
    await _delete_queued(storage, row.id)
    return True


async def drop_queue_of_missing_session(storage_provider: Any, session_id: str) -> int:
    """Delete every queued message of ``session_id``, a session that no longer exists: nothing can deliver them. Returns how many went.

    For the session-messaging sweep (``primer.bus.scheduler_tasks.SessionMessagingSweeper``), which finds sessions through their queued rows.
    The caller has read the session row as gone; a row already deleted by someone else is skipped.
    """
    storage = storage_provider.get_storage(PendingSessionMessage)
    dropped = 0
    while True:
        page = await storage.find(_queued_for(session_id), OffsetPage(offset=0, length=200), order_by=_OLDEST_FIRST)
        rows = list(page.items)
        if not rows:
            return dropped
        for row in rows:
            await _delete_queued(storage, row.id)
            dropped += 1
        if len(rows) < 200:
            return dropped


async def _delete_queued(storage: Any, pending_id: str) -> None:
    """Delete one queued row; one that is already gone (another drain delivered it, a session delete took it) is not an error."""
    try:
        await storage.delete(pending_id)
    except NotFoundError:
        logger.debug("pending_messages: queued row %s was already gone", pending_id)


__all__ = ["PendingQueueFull", "drop_queue_of_missing_session", "has_queued_messages", "realize_next_pending", "store_pending_steer"]
