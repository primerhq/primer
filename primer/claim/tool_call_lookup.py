"""Read a ToolCallTask's own durable TOOL_CALL record, by record_seq.

Phase 3 stage 7a (docs/superpowers/2026-08-29-phase3-execution-topology-design.md,
01a0518b). ``ToolCallTask`` is deliberately ref-only (Q1): it never
carries the tool call's own name/arguments inline, only
``record_seq`` — a pointer to the durable ``TOOL_CALL`` record in the
session's ``messages.jsonl`` that actually has them (see
``ToolCallTask.record_seq``'s own docstring for the ordering invariant
this backs: a task cannot be constructed with a real ``record_seq``
until its ``TOOL_CALL`` record is already durable, so a claiming
worker must never be able to observe a task whose record doesn't
exist yet). This module is the read side of that pointer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.tap.reader import read_record_by_seq

if TYPE_CHECKING:
    from primer.model.tool_call_task import ToolCallTask
    from primer.tap.reader import _WorkspaceReadIO


class ToolCallRecordMismatch(RuntimeError):
    """Raised when a task's record_seq pointer does not resolve cleanly.

    Any of: the log is missing, no record exists at ``record_seq``, the
    record at that seq is not a ``TOOL_CALL``, or its ``payload['id']``
    does not match the task's own ``id``. Every one of these means the
    "record durable before task claimable" ordering invariant broke
    somewhere upstream — a bug worth surfacing loudly, never a case to
    silently paper over with a fallback scan for "the right" record.
    """


async def read_tool_call_record(
    workspace_io: "_WorkspaceReadIO", task: "ToolCallTask",
) -> SessionMessageRecord:
    """Read + verify the TOOL_CALL record ``task.record_seq`` points at.

    Raises :class:`ToolCallRecordMismatch` (never returns ``None``,
    never falls back to scanning for a plausible substitute) if the
    record is missing, isn't a TOOL_CALL, or its id doesn't match
    ``task.id`` — ruling (01a0518b): "fail loudly on mismatch... a
    mismatch means the ordering invariant broke and we want to know."
    """
    record = await read_record_by_seq(
        workspace_io, session_id=task.session_id, seq=task.record_seq,
    )
    if record is None:
        raise ToolCallRecordMismatch(
            f"no record at seq={task.record_seq} for task {task.id!r} "
            f"(session {task.session_id!r}) - a claimable task's own "
            "TOOL_CALL record must already be durable"
        )
    if record.kind != SessionMessageKind.TOOL_CALL:
        raise ToolCallRecordMismatch(
            f"record at seq={task.record_seq} for task {task.id!r} is "
            f"kind={record.kind!r}, expected TOOL_CALL"
        )
    if record.payload.get("id") != task.id:
        raise ToolCallRecordMismatch(
            f"record at seq={task.record_seq} has payload id="
            f"{record.payload.get('id')!r}, expected {task.id!r}"
        )
    return record


__all__ = ["ToolCallRecordMismatch", "read_tool_call_record"]
