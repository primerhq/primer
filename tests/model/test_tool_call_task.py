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


# ---- executor bookkeeping fields (slice S1-B) -------------------------------------------------------


def _task(**extra) -> ToolCallTask:
    return ToolCallTask(
        id="w:tool:1:1", session_id="sess-1", turn_no=1, tool_name="t", record_seq=1,
        created_at=_now(), **extra,
    )


def test_the_executor_bookkeeping_fields_default_to_a_fresh_unowned_row() -> None:
    task = _task()
    assert (task.attempts, task.gate_seq) == (0, 0)
    assert task.claim_token is None
    assert task.materialized_at is None


@pytest.mark.parametrize("field", ["attempts", "gate_seq"])
def test_the_counters_reject_a_negative_value(field) -> None:
    with pytest.raises(ValidationError):
        _task(**{field: -1})


def test_a_row_stored_before_the_fields_existed_still_loads() -> None:
    """Rows written by the park handlers on main carry none of the new keys."""
    stored = _task().model_dump(mode="json")
    for key in ("attempts", "claim_token", "gate_seq", "materialized_at"):
        del stored[key]
    loaded = ToolCallTask.model_validate(stored)
    assert (loaded.attempts, loaded.claim_token, loaded.gate_seq, loaded.materialized_at) == (0, None, 0, None)


def test_the_new_fields_round_trip_through_the_stored_json() -> None:
    task = _task(attempts=3, claim_token="wrk-1:2026-10-05T00:00:00+00:00:ab12cd34", gate_seq=2,
                 materialized_at=_now())
    assert ToolCallTask.model_validate(task.model_dump(mode="json")) == task


# ---- session-qualified identity (S1b) -------------------------------------------------------------------


def test_the_row_id_is_the_session_qualified_scoped_id_and_the_qualification_is_idempotent() -> None:
    from primer.model.tool_call_task import external_call_id, tool_call_task_id

    qualified = tool_call_task_id("sess-1", "x:tool:0:1")
    assert qualified == "sess-1/x:tool:0:1"
    assert tool_call_task_id("sess-1", qualified) == qualified, "qualifying twice does not double the prefix"
    assert tool_call_task_id("sess-2", "x:tool:0:1") != qualified, "two sessions' first calls never share an id"
    assert external_call_id(qualified, "sess-1") == "x:tool:0:1"
    assert external_call_id("x:tool:0:1", "sess-1") == "x:tool:0:1", "a bare scoped id passes through"


def test_only_the_exact_session_prefix_is_stripped() -> None:
    from primer.model.tool_call_task import external_call_id

    assert external_call_id("sess-2/x:tool:0:1", "sess-1") == "sess-2/x:tool:0:1"
    assert external_call_id("a/b:tool:0:1", "sess-1") == "a/b:tool:0:1", "graph node ids are free-form: a slash is not a prefix"
    assert external_call_id("sess-1/a/b:tool:0:1", "sess-1") == "a/b:tool:0:1"


def test_the_task_exposes_the_scoped_id_and_the_id_the_llm_knows_it_by() -> None:
    from primer.model.tool_call_task import tool_call_task_id

    task = ToolCallTask(
        id=tool_call_task_id("sess-1", "x:tool:0:1"), session_id="sess-1", turn_no=0, tool_name="t",
        record_seq=1, created_at=_now(), call_id="call_9f2",
    )
    assert task.scoped_call_id == "x:tool:0:1"
    assert task.wire_call_id == "call_9f2"
    legacy = task.model_copy(update={"call_id": None})
    assert legacy.wire_call_id == "x:tool:0:1", "a row written before call_id existed falls back to the scoped id"


def test_a_row_stored_without_call_id_loads() -> None:
    stored = ToolCallTask(
        id="sess-1/x:tool:0:1", session_id="sess-1", turn_no=0, tool_name="t", record_seq=1, created_at=_now(),
    ).model_dump(mode="json")
    del stored["call_id"]
    assert ToolCallTask.model_validate(stored).call_id is None
