"""Pure contract tests for the versioned task-gate envelope."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic_core import to_jsonable_python

from primer.model.principal import PrincipalRef
from primer.model.responder_source import (
    canonical_responder_payload,
    canonical_responder_source,
)
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.task_gate_runtime import (
    LegacyApprovalTaskGateState,
    TaskGateResolutionReceipt,
    TaskGateResponderReceipt,
    TaskGateState,
    TaskGateStateError,
    decode_task_gate_state,
    gate_state_sha256,
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


def _ask_state() -> TaskGateState:
    now = datetime.now(UTC)
    raised = YieldToWorker(
        Yielded(
            tool_name="ask_user",
            event_key="ask_user:sess-1:call-1",
            timeout=45.0,
            resume_metadata={"question": "colour?"},
            event_keys=None,
        ),
        tool_call_id="call-1",
        llm_messages=[],
    )
    return new_task_gate_state(
        raised,
        task=_task(tool_name="ask_user"),
        gate_seq=1,
        parked_at=now,
        deadline=now + timedelta(seconds=45),
        binding_epoch=7,
        tool_arguments={"question": "colour?"},
    )


def _responder(
    state: TaskGateState,
    *,
    payload=None,
    session_id: str = "sess-1",
    task_id: str = "sess-1/x:tool:1:1",
) -> TaskGateResponderReceipt:
    decision = payload or {"response": "blue"}
    source = canonical_responder_source(
        "ask_user",
        {
            "session_id": session_id,
            "task_id": task_id,
            "occurrence_token": state.occurrence_token,
            "gate_seq": state.gate_seq,
            "event_key": state.event_key,
            "payload_sha256": sha256(
                canonical_responder_payload(decision)
            ).hexdigest(),
        },
    )
    return TaskGateResponderReceipt(
        person=PrincipalRef(
            type="user", id="person-1", display="Person One",
            role="user", source="local", user_id="person-1",
        ),
        responder_epoch="e" * 32,
        domain=source.domain,
        source_identity=source.identity,
        source_key=source.key,
        accepted_at=datetime(2026, 11, 7, tzinfo=UTC),
    )


def _v2_receipt(
    state: TaskGateState,
    *,
    responder: TaskGateResponderReceipt | None,
    payload=None,
) -> TaskGateResolutionReceipt:
    return TaskGateResolutionReceipt.from_jsonable({
        "schema_version": 2,
        "occurrence_token": state.occurrence_token,
        "gate_seq": state.gate_seq,
        "event_key": state.event_key,
        "payload": payload or {"response": "blue"},
        "responder": responder.to_jsonable() if responder is not None else None,
    })


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


def test_v2_approval_remains_readable_without_a_responder() -> None:
    now = datetime.now(UTC)
    state = new_task_gate_state(
        _raised(), task=_task(), gate_seq=1, parked_at=now,
        deadline=now + timedelta(seconds=1), binding_epoch=0,
        tool_arguments={"path": "notes.txt"},
    )
    decoded = TaskGateState.from_jsonable(
        state.with_resolution(_v2_receipt(
            state, responder=None, payload={"decision": "approved"},
        )).to_jsonable()
    )
    assert decoded.resolution is not None
    assert decoded.resolution.schema_version == 2
    assert decoded.resolution.responder is None


def test_v2_ask_user_requires_and_round_trips_one_responder() -> None:
    state = _ask_state()
    responder = _responder(state)
    receipt = _v2_receipt(state, responder=responder)
    decoded = TaskGateState.from_jsonable(
        state.with_resolution(receipt).to_jsonable()
    )
    assert decoded.resolution == receipt
    assert decoded.resolution.responder == responder

    with pytest.raises(TaskGateStateError, match="requires one responder"):
        state.with_resolution(_v2_receipt(state, responder=None))


def test_v1_resolution_is_approval_only() -> None:
    state = _ask_state()
    v1 = make_resolution_receipt(
        occurrence_token=state.occurrence_token,
        gate_seq=state.gate_seq,
        event_key=state.event_key,
        payload={"response": "blue"},
    )
    with pytest.raises(TaskGateStateError, match="approval-only"):
        state.with_resolution(v1)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("domain", "tool_approval", "domain must be ask_user"),
        ("responder_epoch", "E" * 32, "32 lowercase hex"),
        ("accepted_at", "2026-11-07T10:00:00", "timezone"),
        ("source_identity", "json:{bad", "source_identity is invalid"),
        ("source_key", "rs1_" + "0" * 64, "does not match"),
    ],
)
def test_v2_responder_receipt_rejects_malformed_fields(field, value, match) -> None:
    raw = _responder(_ask_state()).to_jsonable()
    raw[field] = value
    with pytest.raises(TaskGateStateError, match=match):
        TaskGateResponderReceipt.from_jsonable(raw)


def test_v2_responder_receipt_rejects_extra_principal_fields() -> None:
    raw = _responder(_ask_state()).to_jsonable()
    raw["person"]["forged"] = "admin"
    with pytest.raises(TaskGateStateError, match="person fields are invalid"):
        TaskGateResponderReceipt.from_jsonable(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("occurrence_token", "f" * 32),
        ("gate_seq", 7),
        ("event_key", "ask_user:other"),
        ("payload_sha256", "0" * 64),
    ],
)
def test_v2_responder_identity_binds_gate_and_decision(field, value) -> None:
    state = _ask_state()
    payload = {"response": "blue"}
    identity = {
        "session_id": "sess-1",
        "task_id": "sess-1/x:tool:1:1",
        "occurrence_token": state.occurrence_token,
        "gate_seq": state.gate_seq,
        "event_key": state.event_key,
        "payload_sha256": sha256(
            canonical_responder_payload(payload)
        ).hexdigest(),
    }
    identity[field] = value
    copied = canonical_responder_source("ask_user", identity)
    responder_raw = _responder(state).to_jsonable()
    responder_raw["source_identity"] = copied.identity
    responder_raw["source_key"] = copied.key
    responder = TaskGateResponderReceipt.from_jsonable(responder_raw)
    receipt = _v2_receipt(state, responder=responder, payload=payload)

    with pytest.raises(TaskGateStateError, match="does not match the decision"):
        state.with_resolution(receipt)


def test_v2_responder_identity_binds_the_exact_task_row() -> None:
    state = _ask_state()
    responder = _responder(
        state, session_id="sess-other", task_id="sess-other/x:tool:1:1",
    )
    resolved = state.with_resolution(
        _v2_receipt(state, responder=responder),
    )
    blob = resolved.to_jsonable()
    task = _task(
        tool_name="ask_user",
        state=ToolCallTaskState.QUEUED,
        claim_token=None,
        gate_occurrence_token=state.occurrence_token,
        gate_event_key=state.event_key,
        gate_until=state.deadline,
        gate_seq=state.gate_seq,
        gate_state=blob,
        gate_state_sha256=gate_state_sha256(blob),
        gate_resolution=resolved.resolution.to_jsonable(),
    )
    with pytest.raises(TaskGateStateError, match="does not match its task row"):
        decode_task_gate_state(blob, task=task)


def test_v2_receipt_is_deep_copy_isolated_and_keeps_typed_payload_equality() -> None:
    state = _ask_state()
    true_payload = {"nested": [{"value": True}]}
    raw = _v2_receipt(
        state, responder=_responder(state, payload=true_payload), payload=true_payload,
    ).to_jsonable()
    decoded = TaskGateResolutionReceipt.from_jsonable(raw)
    raw["payload"]["nested"][0]["value"] = 1
    assert decoded.payload == {"nested": [{"value": True}]}

    integer = _v2_receipt(
        state,
        responder=_responder(state, payload={"nested": [{"value": 1}]}),
        payload={"nested": [{"value": 1}]},
    )
    assert not resolution_receipts_equal(decoded, integer)


def test_gate_state_digest_detects_tampering_but_legacy_missing_digest_reads() -> None:
    state = _ask_state()
    blob = state.to_jsonable()
    task = _task(
        tool_name="ask_user",
        state=ToolCallTaskState.GATED,
        claim_token=None,
        gate_occurrence_token=state.occurrence_token,
        gate_event_key=state.event_key,
        gate_until=state.deadline,
        gate_seq=state.gate_seq,
        gate_state=blob,
        gate_state_sha256=gate_state_sha256(blob),
    )
    assert decode_task_gate_state(blob, task=task) == state
    assert decode_task_gate_state(blob, task=task.model_copy(
        update={"gate_state_sha256": None},
    )) == state

    tampered = state.to_jsonable()
    tampered["yielded"]["resume_metadata"]["question"] = "other"
    with pytest.raises(TaskGateStateError, match="digest does not match"):
        decode_task_gate_state(tampered, task=task.model_copy(
            update={"gate_state": tampered},
        ))


def test_gate_state_digest_accepts_large_and_deep_valid_envelopes() -> None:
    state = _ask_state()
    blob = state.to_jsonable()
    nested: dict[str, object] = {"items": list(range(600))}
    for index in range(32):
        nested = {f"level_{index}": nested}
    blob["yielded"]["resume_metadata"]["large"] = nested

    digest = gate_state_sha256(blob)

    assert len(digest) == 64
    assert digest == gate_state_sha256(blob)


def test_gate_state_digest_canonicalizes_nested_datetime_as_storage_json() -> None:
    state = _ask_state()
    blob = state.to_jsonable()
    accepted_at = datetime(2026, 11, 7, 17, 30, 45, 123456, tzinfo=UTC)
    blob["yielded"]["resume_metadata"]["nested"] = {
        "accepted_at": accepted_at,
    }

    digest = gate_state_sha256(blob)
    stored = json.loads(json.dumps(to_jsonable_python(blob)))

    assert stored["yielded"]["resume_metadata"]["nested"] == {
        "accepted_at": "2026-11-07T17:30:45.123456Z",
    }
    assert gate_state_sha256(stored) == digest


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
