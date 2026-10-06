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

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["reserve_seq"]


async def reserve_seq(
    sessions: Any, session_id: str, *, last_seq: int, where: Mapping[str, Sequence[Any]],
) -> int | None:
    """Reserve seq ``last_seq + 1`` of ``session_id``'s log, or return ``None`` when the guard rejects.

    ``last_seq`` is the value the caller READ; the reservation lands only if the row still holds it (``where`` is the
    caller's own guard, e.g. ``{"turn_status": ["idle"], "parked_status": [None]}``, and must not name ``last_seq``).
    Raises ``NotFoundError`` for a row that is gone.
    """
    if "last_seq" in where:
        raise ValueError("reserve_seq adds its own last_seq term; the caller's guard must not name it")
    written = await sessions.patch_if(
        session_id, {"last_seq": last_seq + 1}, where={**where, "last_seq": [last_seq]},
    )
    return None if written is None else written.last_seq
