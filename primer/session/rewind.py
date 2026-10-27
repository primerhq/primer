"""Rewind as an append-only marker, plus the guards that keep it legal.

Rewind never deletes a line. It appends a rewind_marker naming the seq
to keep, and the read-time replay walk drops the rows past it. The audit
trail survives and the append-only invariant holds, which is why rewind
is auditable but not undoable in v1: the log records that a cut
happened, and readers honour it.

The guards matter more than the write. A rewind into a compacted span
would leave the next turn rebuilding from an EMPTY prompt, because the
folded rows are already gone from the visible set and the summary that
replaced them sits past the target and drops too. That is rejected here,
before anything is appended, rather than special-cased in the walk.

The marker's seq is RESERVED, not seeded from a read (S2a PR-5b, D-A1-02):
the log read takes time and a steer can land in that window, run a turn
and park. The caller re-reads the row under the session's lifecycle lock
and the marker's seq is reserved with a guarded ``patch_if`` (the route's
own idle-unparked guard plus ``last_seq == the row just re-read``,
``primer.session.seq_reservation``); a rejection means the row moved, so
NOTHING is written and the caller answers 409 (a retry re-reads the log).
The reservation is the row's ``last_seq`` write, so there is no later
whole-row write: the old whole-row write of the row read before the log
read repeated the steer's seq and erased its park columns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from primer.model.except_ import ConflictError, ValidationError
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.persistence import WorkspaceMessageWriter
from primer.session.replay import visible_records
from primer.session.seq_reservation import reserve_seq

_USER_INPUT = SessionMessageKind.USER_INPUT.value
_COMPACTION = SessionMessageKind.COMPACTION_MARKER.value


def check_rewind_target(raw_lines: list[str], *, to_seq: int) -> None:
    """Raise unless ``to_seq`` is a legal rewind target.

    Legal means a user_input record that is currently visible, strictly
    after the newest visible compaction marker, and not already the
    newest visible record.

    ConflictError is reserved for the compaction case: that is a state
    conflict the operator resolves by choosing a later target. Every
    other rejection is a malformed target.
    """
    visible = visible_records(raw_lines)
    if not visible:
        raise ValidationError("session has no visible history to rewind")

    marker_seqs = [r["seq"] for r in visible if r.get("kind") == _COMPACTION]
    newest_marker = max(marker_seqs) if marker_seqs else None
    if newest_marker is not None and to_seq <= newest_marker:
        raise ConflictError(
            f"seq {to_seq} is at or behind the latest visible "
            f"compaction_marker (seq={newest_marker}); rewind targets "
            "must lie in post-compaction visibility"
        )

    by_seq = {r.get("seq"): r for r in visible}
    target = by_seq.get(to_seq)
    if target is None:
        raise ValidationError(
            f"seq {to_seq} is not a visible record in this session"
        )
    if target.get("kind") != _USER_INPUT:
        raise ValidationError(
            f"seq {to_seq} is a {target.get('kind')!r} record; rewind "
            "targets must be a user_input"
        )
    newest = max(r["seq"] for r in visible)
    if to_seq >= newest:
        raise ValidationError(
            f"seq {to_seq} is the newest visible record; nothing to discard"
        )


#: The fence of the marker's seq reservation: the route's own up-front guard (an idle, unparked
#: session), checked atomically with ``last_seq == the row the caller read`` by ``reserve_seq``.
_IDLE_UNPARKED: Mapping[str, Sequence[Any]] = {"turn_status": ["idle"], "parked_status": [None]}


async def append_rewind_marker(
    *,
    sessions: Any,
    row: WorkspaceSession,
    workspace_io: Any,
    to_seq: int,
    actor: str,
) -> int:
    """Append the marker AT the reserved seq and return it, the CALLER holding the session's lifecycle lock.

    ``row`` is the row the caller re-read under the lock; the marker's seq is reserved with a guarded
    ``patch_if`` fenced on the route's own up-front guard (an idle, unparked session) plus
    ``last_seq == row.last_seq``, so a steer that ran a turn and parked while the log was being read
    (which set the park columns and moved ``last_seq``) is rejected and NOTHING is written (the caller
    answers 409; a retry re-reads the log) instead of the marker reusing the steer's seq. The
    reservation is the row's ``last_seq`` write, so no row write follows the append.
    """
    seq = await reserve_seq(sessions, row.id, last_seq=row.last_seq, where=_IDLE_UNPARKED)
    if seq is None:
        raise ConflictError(
            f"session {row.id!r} is no longer idle: it moved while the log was being read; the marker "
            "was not written, retry the rewind"
        )
    writer = WorkspaceMessageWriter(
        workspace_io=workspace_io,
        session_id=row.id,
        start_seq=seq - 1,
    )
    appended = await writer.append(SessionMessageRecord(
        seq=1,  # overwritten by the writer's monotonic counter
        kind=SessionMessageKind.REWIND_MARKER,
        payload={"to_seq": to_seq, "actor": actor},
        created_at=datetime.now(UTC),
    ))
    if appended != seq:  # pragma: no cover - the writer was seeded from the reservation
        raise RuntimeError(f"rewind marker of session {row.id}: took seq {appended}, reserved {seq}")
    await writer.flush()
    return seq


__all__ = ["append_rewind_marker", "check_rewind_target"]
