"""Pure contract tests for the versioned task-gate envelope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.task_gate_runtime import (
    LegacyApprovalTaskGateState,
    TaskGateState,
    TaskGateStateError,
    decode_task_gate_state,
    make_resolution_receipt,
    new_task_gate_state,
    resolution_receipts_equal,
)


def _task(**extra) -> ToolCallTask:
    values = {
        "id": "sess-1/x:tool:1:1",
        "session_id": "sess-1",
        "turn_no": 1,
        "tool_name": "workspace__write",
        "state": ToolCallTaskState.RUNNING,
        "record_seq": 1,
        "call_id": "call-1",
        "claim_token": "worker-token",
        "created_at": datetime.now(UTC),
    }
    values.update(extra)
    return ToolCallTask(**values)


def _raised(metadata=None) -> YieldToWorker:
    raised = YieldToWorker(
        Yielded(
            tool_name="_approval",
            event_key="tool_approval:sess-1:call-1",
            timeout=45.0,
            resume_metadata=metadata or {
                "original_call": {
                    "id": "call-1",
                    "name": "workspace__write",
                    "arguments": {"path": "notes.txt"},
                },
            },
            event_keys=["tool_approval:sess-1:call-1", "audit:sess-1"],
        ),
        tool_call_id="call-1",
        llm_messages=[{"role": "assistant", "content": "must not be copied"}],
    )
    return raised


def test_v1_round_trip_is_complete_but_does_not_duplicate_llm_messages() -> None:
    now = datetime.now(UTC)
    raised = _raised()
    state = new_task_gate_state(
        raised,
        task=_task(),
        gate_seq=3,
        parked_at=now,
        deadline=now + timedelta(seconds=45),
        binding_epoch=7,
        tool_arguments={"path": "notes.txt"},
    )

    blob = state.to_jsonable()
    assert "llm_messages" not in blob
    assert blob["yielded"] == {
        "tool_name": "_approval",
        "event_key": "tool_approval:sess-1:call-1",
        "timeout": 45.0,
        "resume_metadata": raised.yielded.resume_metadata,
        "event_keys": ["tool_approval:sess-1:call-1", "audit:sess-1"],
    }
    assert blob["tool_call_id"] == "call-1"
    assert blob["gate_seq"] == 3
    assert len(blob["occurrence_token"]) == 32
    assert blob["binding_epoch"] == 7
    assert TaskGateState.from_jsonable(blob) == state


def test_codec_and_builder_are_deep_copy_isolated() -> None:
    metadata = {
        "original_call": {
            "id": "call-1",
            "name": "workspace__write",
            "arguments": {},
        },
        "nested": {"values": [1]},
    }
    raised = _raised(metadata)
    now = datetime.now(UTC)
    state = new_task_gate_state(
        raised, task=_task(), gate_seq=1, parked_at=now,
        deadline=now + timedelta(seconds=1), binding_epoch=0,
        tool_arguments={},
    )
    metadata["nested"]["values"].append(2)
    blob = state.to_jsonable()
    blob["yielded"]["resume_metadata"]["nested"]["values"].append(3)
    assert state.yielded.resume_metadata["nested"] == {"values": [1]}

    decoded = TaskGateState.from_jsonable(state.to_jsonable())
    source = state.to_jsonable()
    decoded_again = TaskGateState.from_jsonable(source)
    source["yielded"]["resume_metadata"]["nested"]["values"].append(9)
    assert decoded == decoded_again


def test_nonempty_nested_frames_are_refused() -> None:
    raised = _raised()
    raised.frames = [object()]
    with pytest.raises(TaskGateStateError, match="nested continuation frames"):
        new_task_gate_state(
            raised, task=_task(), gate_seq=1, parked_at=datetime.now(UTC),
            deadline=None, binding_epoch=0,
            tool_arguments={"path": "notes.txt"},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_index", 0),
        ("completed_results", []),
        ("remaining_calls", []),
        ("remaining_scoped_call_ids", {}),
    ],
)
def test_other_continuation_state_is_refused(field, value) -> None:
    raised = _raised()
    setattr(raised, field, value)
    with pytest.raises(TaskGateStateError, match=field):
        new_task_gate_state(
            raised, task=_task(), gate_seq=1, parked_at=datetime.now(UTC),
            deadline=None, binding_epoch=0,
            tool_arguments={"path": "notes.txt"},
        )


@pytest.mark.parametrize("field,value", [
    ("id", "call-2"),
    ("name", "workspace__read"),
    ("arguments", {"path": "other.txt"}),
])
def test_approval_original_call_must_match_claimed_call(field, value) -> None:
    raised = _raised()
    raised.yielded.resume_metadata["original_call"][field] = value
    with pytest.raises(TaskGateStateError, match="does not match"):
        new_task_gate_state(
            raised, task=_task(), gate_seq=1, parked_at=datetime.now(UTC),
            deadline=None, binding_epoch=0,
            tool_arguments={"path": "notes.txt"},
        )


@pytest.mark.parametrize("field,value", [
    ("id", "call-2"),
    ("name", "workspace__read"),
    ("arguments", {"path": "other.txt"}),
])
def test_decoded_approval_original_call_must_match_task(field, value) -> None:
    now = datetime.now(UTC)
    task = _task()
    blob = new_task_gate_state(
        _raised(), task=task, gate_seq=1, parked_at=now,
        deadline=now + timedelta(seconds=1), binding_epoch=0,
        tool_arguments={"path": "notes.txt"},
    ).to_jsonable()
    blob["yielded"]["resume_metadata"]["original_call"][field] = value
    gated = task.model_copy(update={
        "state": ToolCallTaskState.GATED,
        "claim_token": None,
        "gate_occurrence_token": blob["occurrence_token"],
        "gate_event_key": blob["event_key"],
        "gate_until": datetime.fromisoformat(blob["deadline"]),
        "gate_seq": 1,
        "gate_state": blob,
    })
    with pytest.raises(TaskGateStateError, match="does not match"):
        decode_task_gate_state(blob, task=gated)


def test_receipt_equality_uses_storage_json_semantics() -> None:
    def receipt(payload):
        return make_resolution_receipt(
            occurrence_token="a" * 32, gate_seq=1, event_key="k", payload=payload,
        )

    assert resolution_receipts_equal(
        receipt({"outer": [{"b": 2, "a": 1}]}),
        receipt({"outer": [{"a": 1.0, "b": 2.0}]}),
    ), "object key order is ignored and JSON numbers compare by value"
    assert not resolution_receipts_equal(receipt({"value": True}), receipt({"value": 1}))


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda blob: blob.update(schema_version=99), "unknown task-gate state"),
        (lambda blob: blob.pop("yielded"), "fields are invalid"),
        (lambda blob: blob.update(occurrence_token="not-a-token"), "32 lowercase hex"),
        (lambda blob: blob["yielded"].update(event_key="another"), "does not match"),
        (lambda blob: blob.update(deadline="not-a-date"), "ISO-8601"),
    ],
)
def test_unknown_or_corrupt_v1_fails_closed(mutate, match) -> None:
    now = datetime.now(UTC)
    blob = new_task_gate_state(
        _raised(), task=_task(), gate_seq=1, parked_at=now,
        deadline=now + timedelta(seconds=1), binding_epoch=0,
        tool_arguments={"path": "notes.txt"},
    ).to_jsonable()
    mutate(blob)
    with pytest.raises(TaskGateStateError, match=match):
        TaskGateState.from_jsonable(blob)


def test_only_an_unambiguous_matching_legacy_approval_is_readable() -> None:
    task = _task(
        state=ToolCallTaskState.GATED,
        claim_token=None,
        gate_event_key="tool_approval:sess-1:call-1",
        gate_until=datetime.now(UTC),
        gate_seq=4,
    )
    metadata = _raised().yielded.resume_metadata
    legacy = decode_task_gate_state(metadata, task=task)
    assert isinstance(legacy, LegacyApprovalTaskGateState)
    assert legacy.tool_call_id == "call-1"
    assert legacy.gate_seq == 4

    wrong = {**metadata, "original_call": {**metadata["original_call"], "id": "call-2"}}
    with pytest.raises(TaskGateStateError, match="does not identify"):
        decode_task_gate_state(wrong, task=task)
    with pytest.raises(TaskGateStateError, match="no unambiguous"):
        decode_task_gate_state({"approval": "pending"}, task=task)
