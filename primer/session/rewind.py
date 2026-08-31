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
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from primer.model.except_ import ConflictError, ValidationError
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
)
from primer.session.persistence import WorkspaceMessageWriter
from primer.session.replay import visible_records

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


async def append_rewind_marker(
    *,
    workspace_io: Any,
    session_id: str,
    start_seq: int,
    to_seq: int,
    actor: str,
) -> int:
    """Append the marker and return its assigned seq."""
    writer = WorkspaceMessageWriter(
        workspace_io=workspace_io,
        session_id=session_id,
        start_seq=start_seq,
    )
    seq = await writer.append(SessionMessageRecord(
        seq=1,  # overwritten by the writer's monotonic counter
        kind=SessionMessageKind.REWIND_MARKER,
        payload={"to_seq": to_seq, "actor": actor},
        created_at=datetime.now(UTC),
    ))
    await writer.flush()
    return seq


__all__ = ["append_rewind_marker", "check_rewind_target"]
