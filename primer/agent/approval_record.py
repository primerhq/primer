"""Build + persist a durable :class:`ToolApprovalRecord` at decision time.

An approval gate exists, while live, only as transient ``parked_state`` on a
session/chat. The instant the decision is finalized (operator approved/
rejected, or a yield timeout/cancel synthesised one) that parked state is
cleared and the call resumes. This module captures the resolved decision into
a persisted row so the Approvals records view can show real history.

Two builders cover the two parked-state shapes:

* :func:`record_from_parked_blob` -- the session/graph ``parked_state`` JSON
  blob (``yielded.resume_metadata.original_call`` + gate fields).
* :func:`record_from_chat_pending` -- the chat ``pending_tool_call`` dict.

:func:`write_approval_record` is best-effort: a failure to persist the record
MUST NOT block or fail a resume, so callers wrap it and it swallows + logs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from primer.model.tool_approval import ToolApprovalRecord


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_from_parked_blob(
    *,
    blob: dict[str, Any],
    decision: str,
    reason: str | None,
    agent_id: str | None = None,
    session_id: str | None = None,
    chat_id: str | None = None,
    requested_at: datetime | None = None,
    decided_at: datetime | None = None,
    decided_by: str | None = None,
    gate_event_key: str | None = None,
) -> ToolApprovalRecord:
    """Build a record from a session/graph ``parked_state`` blob.

    ``blob['yielded']['resume_metadata']`` carries ``original_call`` (the
    gated ``id``/``name``/``arguments``) plus ``policy_id`` /
    ``approval_type`` / ``gate_reason`` and, for ``call_tool`` meta-dispatch,
    ``via_call_tool`` (inner toolset id + principal).

    ``gate_event_key`` defaults to None (backward/forward compatible with
    every existing caller); a caller with a real ``ParkedState.yielded.
    event_key`` in hand should pass it explicitly (01a068da) so the
    respond-time and resume-time write sites can dedupe against the same
    gate via ``ToolApprovalRecord``'s unique index.
    """
    yielded: dict = blob.get("yielded") or {}
    metadata: dict = yielded.get("resume_metadata") or {}
    original: dict = metadata.get("original_call") or {}
    via: dict = metadata.get("via_call_tool") or {}
    return ToolApprovalRecord(
        toolset_id=metadata.get("toolset_id") or via.get("toolset_id"),
        tool_name=original.get("name") or "",
        arguments=original.get("arguments") or {},
        tool_call_id=original.get("id") or blob.get("tool_call_id"),
        gate_event_key=gate_event_key,
        agent_id=agent_id,
        session_id=session_id,
        chat_id=chat_id,
        requested_at=requested_at,
        decided_at=decided_at or _now(),
        decision=decision,  # type: ignore[arg-type]
        reason=reason,
        policy_id=metadata.get("policy_id"),
        approval_type=metadata.get("approval_type"),
        gate_reason=metadata.get("gate_reason"),
        principal=via.get("principal"),
        decided_by=decided_by,
    )


def record_from_chat_pending(
    *,
    pending: dict[str, Any],
    decision: str,
    reason: str | None,
    chat_id: str,
    agent_id: str | None = None,
    requested_at: datetime | None = None,
    decided_at: datetime | None = None,
    decided_by: str | None = None,
) -> ToolApprovalRecord:
    """Build a record from a chat ``pending_tool_call`` dict.

    The chat soft-yield stores ``original_call`` plus the gate fields
    (``policy_id`` / ``approval_type`` / ``gate_reason``) directly on the
    pending dict.
    """
    original: dict = pending.get("original_call") or {}
    return ToolApprovalRecord(
        toolset_id=pending.get("toolset_id"),
        tool_name=original.get("name") or "",
        arguments=original.get("arguments") or {},
        tool_call_id=pending.get("tool_call_id") or original.get("id"),
        agent_id=agent_id,
        session_id=None,
        chat_id=chat_id,
        requested_at=requested_at,
        decided_at=decided_at or _now(),
        decision=decision,  # type: ignore[arg-type]
        reason=reason,
        policy_id=pending.get("policy_id"),
        approval_type=pending.get("approval_type"),
        gate_reason=pending.get("gate_reason"),
        principal=pending.get("principal"),
        decided_by=decided_by,
    )


async def write_approval_record(
    storage: Any | None,
    record: ToolApprovalRecord,
) -> None:
    """Persist a record best-effort. Never raises.

    A failure here must not block or fail an in-progress resume (or a
    respond request), so any exception (including a missing storage) is
    logged and swallowed.

    01a068da: a record with a non-None ``gate_event_key`` can legitimately
    lose a race against the OTHER write site for the same gate (respond-
    time vs. the resume-time fallback) - ``ToolApprovalRecord``'s unique
    index on that field turns the loser's insert into a
    :class:`~primer.model.except_.ConflictError`. That is the mechanism
    working as intended, not a failure: log it at DEBUG (still visible if
    someone is looking, not noise on the normal path) rather than the
    ERROR-level ``logger.exception`` a genuine write failure gets.
    """
    if storage is None:
        return
    from primer.model.except_ import ConflictError

    try:
        await storage.create(record)
    except ConflictError:
        logger.debug(
            "approval-record: gate_event_key=%r already has a record "
            "(the other write site won the race) - skipping",
            record.gate_event_key,
        )
    except Exception:  # noqa: BLE001 - best-effort; resume must not fail
        logger.exception(
            "approval-record: failed to persist record for tool %r",
            record.tool_name,
        )


__all__ = [
    "record_from_chat_pending",
    "record_from_parked_blob",
    "write_approval_record",
]
