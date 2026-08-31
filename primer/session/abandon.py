"""Closing a session's open gate without answering it.

A parked session is waiting on a human. Sometimes that wait has to end
without an answer: the binding is being switched away from the agent
that raised the gate, or the turn is being cancelled outright.

This is one helper rather than three call sites because the ORDER is an
invariant. The log must never carry an unpaired tool_use, so a synthetic
rejected tool_result is written first, then the terminal that closes the
turn. Ported from chat's chokepoint, which exists for the same reason.

Clearing parked_state as well as parked_status matters just as much: a
stale parked_session subscription that fires afterwards must find no
park to resume, so it skips and self-deletes rather than reviving a turn
that has already been closed cancelled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.persistence import WorkspaceMessageWriter


async def abandon_session_gate(
    *,
    sessions: Any,
    workspace_io: Any,
    row: WorkspaceSession,
    reason: str,
    approval_records: Any | None = None,
) -> WorkspaceSession:
    """Close an open gate as rejected and clear the park.

    Returns the updated row. A session with nothing parked is left
    untouched, so callers may invoke this unconditionally.
    """
    parked = row.parked_state or {}
    if row.parked_status is None and not parked:
        return row

    tool_call_id = parked.get("tool_call_id")
    mode = parked.get("mode") or (
        (parked.get("yielded") or {}).get("tool_name")
    )
    result_text = f"gate abandoned: {reason}"

    # Best-effort, and before the records: an approval that was resolved
    # by abandonment should survive even if the log write then fails.
    if approval_records is not None and mode == "approval":
        try:
            from primer.agent.approval_record import (
                record_from_chat_pending,
                write_approval_record,
            )

            await write_approval_record(
                approval_records,
                record_from_chat_pending(
                    pending=parked,
                    decision="cancelled",
                    reason=result_text,
                    chat_id=row.id,
                    agent_id=getattr(row.binding, "agent_id", None),
                    requested_at=row.created_at,
                ),
            )
        except Exception:  # noqa: BLE001 - the gate still has to close
            pass

    writer = WorkspaceMessageWriter(
        workspace_io=workspace_io, session_id=row.id, start_seq=row.last_seq,
    )
    now = datetime.now(UTC)
    # The pairing record comes first: a terminal written before it would
    # leave a tool_use no result ever answers. Payload shape MUST match
    # the live-turn write (primer/session/persistence.py's
    # _ExecutorToolResult handler: call_id/output/error) - timeline.py's
    # TOOL_CALL pairing looks up ``payload["call_id"]`` specifically, so
    # the old {id, name, result} shape here silently never closed its
    # TOOL_CALL in the trace/timeline (01a05350).
    await writer.append(SessionMessageRecord(
        seq=1,  # overwritten by the writer's monotonic counter
        kind=SessionMessageKind.TOOL_RESULT,
        payload={
            "call_id": tool_call_id,
            "output": result_text,
            "error": True,
        },
        created_at=now,
    ))
    last_seq = await writer.append(SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.CANCELLED,
        payload={"reason": reason},
        created_at=now,
    ))
    await writer.flush()

    updated = row.model_copy(update={
        "parked_status": None,
        "parked_state": None,
        "parked_event_key": None,
        "last_seq": last_seq,
    })
    await sessions.update(updated)
    return updated


__all__ = ["abandon_session_gate"]
