"""Writing a canvas record into the session's message log (pivot P5, Task 6; plan Decision 2).

A ``canvas_op`` or ``canvas_event`` record is appended to the session's ``messages.jsonl``, which has ONE writer at a time per
session, and who that is depends on whether a turn is running:

* **Inside a turn** (an agent applies a batch from a tool call) the turn's own writer owns the seqs. The record goes through it,
  reached through the delegation recorder the dispatch publishes on a contextvar
  (``primer.session.delegation.current_delegation_sink``), so it takes the next seq of the turn's counter and lands in order.
* **Outside a turn** (an operator's batch, a clear, a user's click) the record RESERVES the next seq on the session row first
  (``primer.session.seq_reservation.reserve_seq``), guarded on the turn not running, and is then appended at that seq. The
  guard is the point: a turn's writer writes ``last_seq`` back only at its exits, so a seq reserved while a turn is running would be
  one the turn has already numbered (the documented gap of the PAUSE_SUPERSEDED records).
* **While a turn is running and the caller is not the turn** nothing is written. The batch or the event is still in the canvas row
  and in the platform event log, and a silent event reaches the agent in the next digest, so only the transcript line is skipped.

A record is advisory: a canvas that has been written must not fail because its transcript line could not be, so
:func:`append_canvas_record` never raises. It logs a WARNING and returns ``None``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord, WorkspaceSession
from primer.session.delegation import current_delegation_sink
from primer.session.persistence import WorkspaceMessageWriter
from primer.session.seq_reservation import reserve_seq

logger = logging.getLogger(__name__)

#: How many times a reservation is tried: each rejection means a steer or a park moved the row between the read and the patch.
_RESERVE_ATTEMPTS = 5

#: The ``turn_status`` values a record may be reserved under: the turn's writer is not numbering seqs.
_NO_TURN_RUNNING = ["idle", "claimable"]


async def append_canvas_record(
    *,
    storage_provider: Any,
    workspace_registry: Any,
    event_bus: Any,
    session_id: str,
    workspace_id: str,
    kind: SessionMessageKind,
    payload: dict[str, Any],
) -> int | None:
    """Append one canvas record to ``session_id``'s log and return its seq, or ``None`` when it was not written.

    ``None`` means: the call was abandoned by a Stop, no registry was given, the session is gone, a turn is running, no seq could
    be reserved, or the write failed (a WARNING is logged for the last two and for a failure). Never raises.
    """
    try:
        sink = current_delegation_sink()
        if sink is not None and getattr(sink, "session_id", None) == session_id:
            return await sink.append_turn_record(kind, payload)
        if workspace_registry is None:
            return None
        return await _append_outside_a_turn(
            storage_provider=storage_provider, workspace_registry=workspace_registry, event_bus=event_bus,
            session_id=session_id, workspace_id=workspace_id, kind=kind, payload=payload,
        )
    except Exception:  # noqa: BLE001 - advisory: the canvas is already written
        logger.warning("canvas record %s for session %s could not be written", kind, session_id, exc_info=True)
        return None


async def _append_outside_a_turn(
    *,
    storage_provider: Any,
    workspace_registry: Any,
    event_bus: Any,
    session_id: str,
    workspace_id: str,
    kind: SessionMessageKind,
    payload: dict[str, Any],
) -> int | None:
    sessions = storage_provider.get_storage(WorkspaceSession)
    # The workspace first: a reservation whose record is never appended leaves a gap in the seqs.
    workspace = await workspace_registry.get_workspace(workspace_id)
    reserved: int | None = None
    for _ in range(_RESERVE_ATTEMPTS):
        row = await sessions.get(session_id)
        if row is None:
            return None
        if row.turn_status == "running":
            logger.debug("session %s: a turn is running; the canvas record is skipped", session_id)
            return None
        try:
            reserved = await reserve_seq(sessions, session_id, last_seq=row.last_seq, where={"turn_status": _NO_TURN_RUNNING})
        except NotFoundError:
            return None                      # the session was deleted between the read and the reservation
        if reserved is not None:
            break
    if reserved is None:
        logger.warning("session %s: no seq could be reserved for a canvas record after %d attempts", session_id, _RESERVE_ATTEMPTS)
        return None
    writer = WorkspaceMessageWriter(workspace_io=workspace, session_id=session_id, start_seq=reserved - 1)
    seq = await writer.append(SessionMessageRecord(
        seq=1,  # overwritten by the writer's monotonic counter
        kind=kind,
        payload=payload,
        created_at=datetime.now(UTC),
    ))
    await writer.flush()
    if event_bus is not None:
        try:
            await event_bus.publish(f"session:{session_id}:tick", {"seq": seq})
        except Exception:  # noqa: BLE001 - advisory: the record is written; a console catches up on its next read
            logger.warning("session %s: the tick for canvas record %d could not be published", session_id, seq, exc_info=True)
    return seq


__all__ = ["append_canvas_record"]
