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

from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.workspace_session import (
    PendingSessionMessage,
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.enqueue import wake_session
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
    """
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
    await storage_provider.get_storage(PendingSessionMessage).create(row)
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
    """Drop the oldest queued steer(s) once the session exceeds the cap.

    The drop must never be silent -- that would reintroduce the exact
    defect this task exists to remove, one layer down. Every drop writes
    a PAUSE_SUPERSEDED(action="dropped") record naming the dropped text,
    so an operator asking "did that ever arrive" can find the answer.
    """
    storage = storage_provider.get_storage(PendingSessionMessage)
    page = await storage.find(
        Predicate(
            left=FieldRef(name="session_id"), op=Op.EQ,
            right=Value(value=session.id),
        ),
        # Fetches a generous ceiling, not just cap+1: this runs after
        # every single store_pending_steer call, so the excess is
        # normally exactly one row -- but fetching more stays correct
        # even if something else ever inserts a burst directly (a lowered
        # cap in a future change, a bug elsewhere), trimming it back down
        # to the cap in one pass instead of silently leaving the rest.
        # OffsetPage.length is itself capped at 200 storage-wide, so this
        # can't just keep multiplying the cap for "more margin".
        OffsetPage(offset=0, length=min(_MAX_PENDING_PER_SESSION * 3, 200)),
        order_by=[
            OrderBy(field="enqueued_at", direction="asc"),
            OrderBy(field="id", direction="asc"),
        ],
    )
    rows = list(page.items)
    if len(rows) <= _MAX_PENDING_PER_SESSION:
        return
    for row in rows[: len(rows) - _MAX_PENDING_PER_SESSION]:
        await storage.delete(row.id)
        text = "\n".join(
            p.get("text", "") for p in row.parts
            if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
        )
        await _record_dropped_pending(
            workspace_registry=workspace_registry,
            event_bus=event_bus,
            storage_provider=storage_provider,
            session=session,
            dropped_text=text,
            enqueued_at=row.enqueued_at,
        )


async def _record_dropped_pending(
    *,
    workspace_registry: Any,
    event_bus: Any | None,
    storage_provider: Any,
    session: WorkspaceSession,
    dropped_text: str,
    enqueued_at: datetime,
) -> None:
    """Best-effort announcement of a capacity-dropped pending message.

    Advisory, like every other tick publish in this module's family: a
    write failure here must not block the steer that triggered it, but it
    IS logged (unlike a plain advisory) because a swallowed exception here
    would defeat the entire point of this function.

    The record's seq is RESERVED on the row before it is written (``session`` is the caller's snapshot, possibly stale, and the
    row's ``last_seq`` has to move so the next writer does not repeat the seq: ticket 01a11cd8).
    """
    if workspace_registry is None:
        return
    try:
        ws = await workspace_registry.get_workspace(session.workspace_id)
        if ws is None:
            return
        reserved = await reserve_next_seq(storage_provider.get_storage(WorkspaceSession), session.id)
        if reserved is None:
            logger.warning("pending_messages: no seq could be reserved to record a dropped pending message for session %s", session.id)
            return
        writer = WorkspaceMessageWriter(
            workspace_io=ws, session_id=session.id, start_seq=reserved - 1,
        )
        seq = await writer.append(SessionMessageRecord(
            seq=1,  # overwritten by the writer's monotonic counter
            kind=SessionMessageKind.PAUSE_SUPERSEDED,
            payload={
                "action": "dropped",
                "text": dropped_text,
                "enqueued_at": enqueued_at.isoformat(),
                "reason": f"pending queue exceeded {_MAX_PENDING_PER_SESSION}",
            },
            created_at=datetime.now(UTC),
        ))
        await writer.flush()
    except Exception:  # noqa: BLE001 -- advisory, never block the steer
        logger.exception(
            "pending_messages: failed to record a dropped pending message "
            "for session %s", session.id,
        )
        return
    if event_bus is not None:
        try:
            await event_bus.publish(f"session:{session.id}:tick", {"seq": seq})
        except Exception:  # noqa: BLE001 -- advisory
            logger.exception(
                "pending_messages: failed to publish dropped-pending tick "
                "for session %s", session.id,
            )


async def realize_next_pending(
    *,
    storage_provider: Any,
    workspace_id: str,
    session_id: str,
    wake_deps: Any,
) -> bool:
    """Realize the oldest queued steer into a real turn.

    Returns True when a row was realized and the session woken. The row
    is deleted before the wake so a crash between the two loses the
    follow-up rather than replaying it forever.
    """
    storage = storage_provider.get_storage(PendingSessionMessage)
    page = await storage.find(
        Predicate(
            left=FieldRef(name="session_id"), op=Op.EQ,
            right=Value(value=session_id),
        ),
        OffsetPage(offset=0, length=1),
        order_by=[
            OrderBy(field="enqueued_at", direction="asc"),
            OrderBy(field="id", direction="asc"),
        ],
    )
    rows = list(page.items)
    if not rows:
        return False
    row = rows[0]
    text = "\n".join(
        p.get("text", "") for p in row.parts
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
    )
    await storage.delete(row.id)
    if not text:
        # Reaped rather than left at the head of the queue, where it
        # would block every later follow-up behind an empty wake.
        return False
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
        deps=wake_deps,
    )
    return True


__all__ = ["realize_next_pending", "store_pending_steer"]
