"""Unit tests for primer.model.tool_call_task.ToolCallTask.

Phase 3 stage 7a (01a0518b). Pins the record_seq ordering invariant
(see the field's own docstring): a ToolCallTask cannot be constructed
without a record_seq, because it cannot be constructed until the
TOOL_CALL record it points at is already durable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_record_seq_is_required() -> None:
    with pytest.raises(ValidationError, match="record_seq"):
        ToolCallTask(
            id="w:tool:1:1",
            session_id="sess-1",
            turn_no=1,
            tool_name="workspace__write",
            created_at=_now(),
        )


def test_record_seq_rejects_non_positive() -> None:
    """seq is 1-based (SessionMessageRecord.seq: ge=1) - record_seq
    mirrors that floor."""
    with pytest.raises(ValidationError):
        ToolCallTask(
            id="w:tool:1:1",
            session_id="sess-1",
            turn_no=1,
            tool_name="workspace__write",
            record_seq=0,
            created_at=_now(),
        )


def test_constructs_with_a_valid_record_seq() -> None:
    task = ToolCallTask(
        id="w:tool:1:1",
        session_id="sess-1",
        turn_no=1,
        tool_name="workspace__write",
        record_seq=5,
        created_at=_now(),
    )
    assert task.record_seq == 5
    assert task.state == ToolCallTaskState.QUEUED
    assert task.gate_state is None
    assert task.result_state is None
