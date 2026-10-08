"""Reserving the next seq of a session's message log before writing a record at it.

A record appended to ``messages.jsonl`` takes ``last_seq + 1``, and the session row's ``last_seq`` is what the next
writer (``wake_session``'s USER_INPUT, a turn's writer, a marker) seeds from. A writer that reads the row, appends at the
read value and writes the row back whole races every other writer: its record repeats a seq a steer just took, and its
write erases the steer's ``last_seq`` and armed turn. The protocol that closes that (plan 3.8 A3):

1. RESERVE: a guarded ``patch_if({"last_seq": old + 1})`` whose ``where`` carries the caller's guard AND ``last_seq == old``;
   a rejection means the row changed since the caller read it and NOTHING has been written;
2. append the record with the reserved seq and flush;
3. finish with ONE more guarded ``patch_if`` of the fields the record's meaning needs (the caller's own).

The reservation is the ``last_seq`` write, so there is no later whole-row write. Callers hold ``session_lifecycle_lock``
(this module never takes it: the route callers already hold the non-reentrant lock) and bound steps 2 and 3 with
``mutation_lock.IN_LOCK_IO_TIMEOUT_S``. A step-1 rejection leaves nothing behind; a timeout after step 1 leaves a reserved gap
in the seqs (a seq nobody wrote), which readers already tolerate.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from primer.model.except_ import NotFoundError

logger = logging.getLogger(__name__)

__all__ = ["advance_last_seq", "reserve_next_seq", "reserve_seq"]


async def reserve_seq(
    sessions: Any, session_id: str, *, last_seq: int, where: Mapping[str, Sequence[Any]], conn: Any | None = None,
) -> int | None:
    """Reserve seq ``last_seq + 1`` of ``session_id``'s log, or return ``None`` when the guard rejects.

    ``last_seq`` is the value the caller READ; the reservation lands only if the row still holds it (``where`` is the
    caller's own guard, e.g. ``{"turn_status": ["idle"], "parked_status": [None]}``, and must not name ``last_seq``).
    ``conn`` is the transaction the caller is inside, if any (the claim adapter's release), so the patch is part of it
    and does not wait on a row lock that transaction holds. Raises ``NotFoundError`` for a row that is gone.
    """
    if "last_seq" in where:
        raise ValueError("reserve_seq adds its own last_seq term; the caller's guard must not name it")
    extra = {} if conn is None else {"conn": conn}
    written = await sessions.patch_if(
        session_id, {"last_seq": last_seq + 1}, where={**where, "last_seq": [last_seq]}, **extra,
    )
    return None if written is None else written.last_seq


async def advance_last_seq(sessions: Any, session_id: str, seq: int, *, attempts: int = 5) -> bool:
    """Raise ``last_seq`` of ``session_id`` to ``seq``; never lower it. True when this call wrote it.

    A writer that has numbered records of its own (a turn's writer, a resume drain's) tells the row where it got to, so the next writer
    seeds past them. ONE field-scoped ``patch_if`` of ``last_seq`` alone, fenced on the value just read: a writer that moved the row
    since (a steer took a higher seq, a park wrote its columns) is never written over, which a ``get`` + whole-document ``update`` from
    that snapshot cannot promise. A rejected fence means the row changed under us, so it is read again and decided again (a row that
    is now at or past ``seq`` is left alone); after ``attempts`` rejections the call gives up and logs, because the writers that keep
    winning the race are advancing ``last_seq`` themselves. A row that is gone, or already at or past ``seq``, is not an error.
    """
    for _ in range(attempts):
        fresh = await sessions.get(session_id)
        if fresh is None or fresh.last_seq >= seq:
            return False
        written = await sessions.patch_if(session_id, {"last_seq": seq}, where={"last_seq": [fresh.last_seq]})
        if written is not None:
            return True
    logger.warning(
        "session %s: last_seq stayed behind %d after %d rejected fences (another writer kept moving the row)", session_id, seq, attempts,
    )
    return False


async def reserve_next_seq(sessions: Any, session_id: str, *, conn: Any | None = None, attempts: int = 5) -> int | None:
    """Reserve the next seq of ``session_id``'s log for an ADVISORY record that has no guard of its own, or ``None``.

    For a writer that is not part of a turn or a state change and only announces something (the release's terminal marker, a
    PAUSE_SUPERSEDED record): it reads the row, reserves ``last_seq + 1`` with :func:`reserve_seq` and, when the row moved
    in between (a steer took that seq), reads again and reserves the next. The record is then appended with
    ``start_seq = reserved - 1``. ``None`` when the row is gone or ``attempts`` reservations were each overtaken; the caller
    skips its record (it is advisory) and nothing was written. A reservation whose record is never appended leaves a gap in the
    seqs, which readers already tolerate.
    """
    extra = {} if conn is None else {"conn": conn}
    for _ in range(attempts):
        fresh = await sessions.get(session_id, **extra)
        if fresh is None:
            return None
        try:
            reserved = await reserve_seq(sessions, session_id, last_seq=fresh.last_seq, where={}, conn=conn)
        except NotFoundError:
            return None
        if reserved is not None:
            return reserved
    logger.warning("session %s: no seq could be reserved for an advisory record after %d attempts", session_id, attempts)
    return None
