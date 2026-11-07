"""Typed persistence contract for one task-level yielding-tool gate.

This is the internal, default-off A1 foundation. It owns the versioned gate
envelope, its occurrence identity and the deterministic decision receipt used
by the guarded GATED -> QUEUED transition. Public decision surfaces and
resolved executor consumption are deliberately outside this slice.
"""

from __future__ import annotations

import copy
import math
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic_core import to_jsonable_python

from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.yield_ import Yielded, YieldToWorker
from primer.storage._patch import json_equal


TASK_GATE_STATE_VERSION = 1
TASK_GATE_RESOLUTION_VERSION = 1
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")


class TaskGateStateError(ValueError):
    """A task gate cannot be encoded or decoded safely."""


class TaskGateDecisionClass(StrEnum):
    """How a stored task row relates to one proposed decision receipt."""

    PENDING = "pending"
    IDENTICAL = "identical"
    CONFLICT = "conflict"
    STALE = "stale"
    RUNNING = "running"
    TERMINAL = "terminal"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class TaskGateResolutionReceipt:
    """Deterministic receipt of the decision for exactly one gate occurrence."""

    occurrence_token: str
    gate_seq: int
    event_key: str
    payload: dict[str, Any]
    schema_version: int = TASK_GATE_RESOLUTION_VERSION

    def to_jsonable(self) -> dict[str, Any]:
        return copy.deepcopy({
            "schema_version": self.schema_version,
            "occurrence_token": self.occurrence_token,
            "gate_seq": self.gate_seq,
            "event_key": self.event_key,
            "payload": self.payload,
        })

    @classmethod
    def from_jsonable(cls, raw: Any) -> "TaskGateResolutionReceipt":
        data = _mapping(raw, "task-gate resolution")
        _exact_keys(
            data,
            {
                "schema_version", "occurrence_token", "gate_seq",
                "event_key", "payload",
            },
            "task-gate resolution",
        )
        version = _integer(data["schema_version"], "resolution schema_version", minimum=1)
        if version != TASK_GATE_RESOLUTION_VERSION:
            raise TaskGateStateError(
                f"unknown task-gate resolution schema_version {version!r}; "
                f"this runtime understands {TASK_GATE_RESOLUTION_VERSION}"
            )
        return cls(
            occurrence_token=_token(data["occurrence_token"]),
            gate_seq=_integer(data["gate_seq"], "resolution gate_seq", minimum=1),
            event_key=_nonempty_text(data["event_key"], "resolution event_key"),
            payload=copy.deepcopy(_mapping(data["payload"], "resolution payload")),
            schema_version=version,
        )


@dataclass(frozen=True)
class TaskGateState:
    """Version-1 gate envelope for a flat claimed tool-call task."""

    occurrence_token: str
    gate_seq: int
    event_key: str
    yielded: Yielded
    tool_call_id: str
    tool_arguments: dict[str, Any]
    parked_at: datetime
    deadline: datetime | None
    binding_epoch: int | None
    resolution: TaskGateResolutionReceipt | None = None
    schema_version: int = TASK_GATE_STATE_VERSION

    def with_resolution(
        self, receipt: TaskGateResolutionReceipt,
    ) -> "TaskGateState":
        _receipt_matches_state(receipt, self)
        return replace(self, resolution=receipt)

    def to_jsonable(self) -> dict[str, Any]:
        """Return an isolated JSON-safe envelope with no LLM-message copy."""
        return copy.deepcopy({
            "schema_version": self.schema_version,
            "occurrence_token": self.occurrence_token,
            "gate_seq": self.gate_seq,
            "event_key": self.event_key,
            "yielded": {
                "tool_name": self.yielded.tool_name,
                "event_key": self.yielded.event_key,
                "timeout": self.yielded.timeout,
                "resume_metadata": self.yielded.resume_metadata,
                "event_keys": self.yielded.event_keys,
            },
            "tool_call_id": self.tool_call_id,
            "tool_arguments": self.tool_arguments,
            "parked_at": self.parked_at.isoformat(),
            "deadline": self.deadline.isoformat() if self.deadline is not None else None,
            "binding_epoch": self.binding_epoch,
            "resolution": (
                self.resolution.to_jsonable()
                if self.resolution is not None else None
            ),
        })

    @classmethod
    def from_jsonable(cls, raw: Any) -> "TaskGateState":
        data = _mapping(raw, "task-gate state")
        _exact_keys(
            data,
            {
                "schema_version", "occurrence_token", "gate_seq", "event_key",
                "yielded", "tool_call_id", "tool_arguments", "parked_at", "deadline",
                "binding_epoch", "resolution",
            },
            "task-gate state",
        )
        version = _integer(data["schema_version"], "state schema_version", minimum=1)
        if version != TASK_GATE_STATE_VERSION:
            raise TaskGateStateError(
                f"unknown task-gate state schema_version {version!r}; "
                f"this runtime understands {TASK_GATE_STATE_VERSION}"
            )
        yielded = _yielded(data["yielded"])
        event_key = _nonempty_text(data["event_key"], "state event_key")
        if yielded.event_key != event_key:
            raise TaskGateStateError("task-gate yielded event_key does not match its envelope")
        receipt = (
            TaskGateResolutionReceipt.from_jsonable(data["resolution"])
            if data["resolution"] is not None else None
        )
        state = cls(
            occurrence_token=_token(data["occurrence_token"]),
            gate_seq=_integer(data["gate_seq"], "state gate_seq", minimum=1),
            event_key=event_key,
            yielded=yielded,
            tool_call_id=_nonempty_text(data["tool_call_id"], "state tool_call_id"),
            tool_arguments=copy.deepcopy(_mapping(data["tool_arguments"], "state tool_arguments")),
            parked_at=_aware_datetime(data["parked_at"], "state parked_at"),
            deadline=(
                _aware_datetime(data["deadline"], "state deadline")
                if data["deadline"] is not None else None
            ),
            binding_epoch=(
                _integer(data["binding_epoch"], "state binding_epoch", minimum=0)
                if data["binding_epoch"] is not None else None
            ),
            resolution=receipt,
            schema_version=version,
        )
        if state.deadline is not None and state.deadline < state.parked_at:
            raise TaskGateStateError("task-gate deadline precedes parked_at")
        if receipt is not None:
            _receipt_matches_state(receipt, state)
        _validate_approval_original_call(state)
        return state


@dataclass(frozen=True)
class LegacyApprovalTaskGateState:
    """Read-only view of the one safe legacy shape; never decision-eligible."""

    event_key: str
    gate_seq: int
    yielded: Yielded
    tool_call_id: str
    deadline: datetime | None
    schema_version: int = 0


TaskGateDecodedState = TaskGateState | LegacyApprovalTaskGateState


def new_task_gate_state(
    raised: YieldToWorker,
    *,
    task: ToolCallTask,
    gate_seq: int,
    parked_at: datetime,
    deadline: datetime | None,
    binding_epoch: int | None,
    tool_arguments: dict[str, Any],
) -> TaskGateState:
    """Build one fresh flat-task envelope, refusing nested continuation state."""
    if getattr(raised, "frames", None):
        raise TaskGateStateError(
            "task-level gates do not support nested continuation frames"
        )
    for field_name in (
        "batch_index", "completed_results", "remaining_calls",
        "remaining_scoped_call_ids",
    ):
        if getattr(raised, field_name, None) is not None:
            raise TaskGateStateError(
                f"task-level gates do not support YieldToWorker.{field_name}"
            )
    if raised.tool_call_id != task.wire_call_id:
        raise TaskGateStateError(
            "the yielded call id does not identify the claimed tool-call task"
        )
    yielded = _copy_yielded(raised.yielded)
    state = TaskGateState(
        occurrence_token=secrets.token_hex(16),
        gate_seq=_integer(gate_seq, "state gate_seq", minimum=1),
        event_key=_nonempty_text(yielded.event_key, "state event_key"),
        yielded=yielded,
        tool_call_id=_nonempty_text(raised.tool_call_id, "state tool_call_id"),
        tool_arguments=copy.deepcopy(_mapping(
            to_jsonable_python(tool_arguments), "state tool_arguments",
        )),
        parked_at=_ensure_aware(parked_at, "state parked_at"),
        deadline=(
            _ensure_aware(deadline, "state deadline")
            if deadline is not None else None
        ),
        binding_epoch=(
            _integer(binding_epoch, "state binding_epoch", minimum=0)
            if binding_epoch is not None else None
        ),
    )
    _validate_approval_original_call(state, expected_tool_name=task.tool_name)
    # Round-trip through the decoder so one writer and every reader enforce
    # exactly the same shape, including finite timeout and deadline ordering.
    return TaskGateState.from_jsonable(state.to_jsonable())


def decode_task_gate_state(
    raw: Any,
    *,
    task: ToolCallTask | None = None,
) -> TaskGateDecodedState:
    """Decode a v1 envelope, or the one unambiguous legacy approval shape."""
    data = _mapping(raw, "task-gate state")
    if "schema_version" in data:
        state = TaskGateState.from_jsonable(data)
        if task is not None:
            _state_matches_task(state, task)
        return state
    if task is None:
        raise TaskGateStateError(
            "legacy task-gate approval metadata requires its task row"
        )
    return _decode_legacy_approval(data, task)


def make_resolution_receipt(
    *, occurrence_token: str, gate_seq: int, event_key: str,
    payload: dict[str, Any] | None,
) -> TaskGateResolutionReceipt:
    """Build the canonical receipt used both in the envelope and top-level field."""
    encoded = to_jsonable_python(copy.deepcopy(payload or {}))
    receipt = TaskGateResolutionReceipt(
        occurrence_token=_token(occurrence_token),
        gate_seq=_integer(gate_seq, "resolution gate_seq", minimum=1),
        event_key=_nonempty_text(event_key, "resolution event_key"),
        payload=copy.deepcopy(_mapping(encoded, "resolution payload")),
    )
    return TaskGateResolutionReceipt.from_jsonable(receipt.to_jsonable())


def classify_task_gate_decision(
    task: ToolCallTask,
    receipt: TaskGateResolutionReceipt,
) -> TaskGateDecisionClass:
    """Classify without mutating; corrupt or legacy gates fail closed."""
    if task.state in (ToolCallTaskState.DONE, ToolCallTaskState.FAILED):
        return TaskGateDecisionClass.TERMINAL
    exact_occurrence = (
        task.gate_occurrence_token == receipt.occurrence_token
        and task.gate_seq == receipt.gate_seq
        and task.gate_event_key == receipt.event_key
    )
    if not exact_occurrence:
        return TaskGateDecisionClass.STALE
    if task.state is ToolCallTaskState.RUNNING or task.claim_token is not None:
        return TaskGateDecisionClass.RUNNING
    try:
        decoded = decode_task_gate_state(task.gate_state, task=task)
    except TaskGateStateError:
        return TaskGateDecisionClass.CORRUPT
    if not isinstance(decoded, TaskGateState):
        return TaskGateDecisionClass.CORRUPT
    raw_receipt = task.gate_resolution
    if task.state is ToolCallTaskState.GATED:
        if raw_receipt is not None or decoded.resolution is not None:
            return TaskGateDecisionClass.CORRUPT
        return TaskGateDecisionClass.PENDING
    if task.state is not ToolCallTaskState.QUEUED:
        return TaskGateDecisionClass.STALE
    try:
        stored = TaskGateResolutionReceipt.from_jsonable(raw_receipt)
    except TaskGateStateError:
        return TaskGateDecisionClass.CORRUPT
    if decoded.resolution is None or not resolution_receipts_equal(decoded.resolution, stored):
        return TaskGateDecisionClass.CORRUPT
    return (
        TaskGateDecisionClass.IDENTICAL
        if resolution_receipts_equal(stored, receipt) else TaskGateDecisionClass.CONFLICT
    )


def _decode_legacy_approval(
    metadata: dict[str, Any], task: ToolCallTask,
) -> LegacyApprovalTaskGateState:
    original = metadata.get("original_call")
    if not isinstance(original, dict):
        raise TaskGateStateError(
            "legacy task-gate metadata has no unambiguous original_call"
        )
    call_id = original.get("id")
    name = original.get("name")
    arguments = original.get("arguments")
    if (
        call_id != task.wire_call_id
        or name != task.tool_name
        or not isinstance(arguments, dict)
        or not isinstance(task.gate_event_key, str)
        or not task.gate_event_key.startswith("tool_approval:")
    ):
        raise TaskGateStateError(
            "legacy task-gate approval metadata does not identify this task"
        )
    copied = copy.deepcopy(metadata)
    return LegacyApprovalTaskGateState(
        event_key=task.gate_event_key,
        gate_seq=task.gate_seq,
        yielded=Yielded(
            tool_name="_approval",
            event_key=task.gate_event_key,
            timeout=None,
            resume_metadata=copied,
            event_keys=None,
        ),
        tool_call_id=call_id,
        deadline=task.gate_until,
    )


def _state_matches_task(state: TaskGateState, task: ToolCallTask) -> None:
    validate_task_gate_call(state, task)
    if (
        state.occurrence_token != task.gate_occurrence_token
        or state.gate_seq != task.gate_seq
        or state.event_key != task.gate_event_key
        or state.tool_call_id != task.wire_call_id
    ):
        raise TaskGateStateError("task-gate envelope does not match its task row")
    if state.deadline != task.gate_until:
        raise TaskGateStateError("task-gate deadline does not match its task row")
    raw_receipt = task.gate_resolution
    if state.resolution is None and raw_receipt is None:
        return
    if state.resolution is None or raw_receipt is None:
        raise TaskGateStateError("task-gate resolution mirrors disagree")
    if not resolution_receipts_equal(
        state.resolution, TaskGateResolutionReceipt.from_jsonable(raw_receipt),
    ):
        raise TaskGateStateError("task-gate resolution mirrors disagree")


def validate_task_gate_call(state: TaskGateState, task: ToolCallTask) -> None:
    """Validate the persisted call identity against its ref-only task row."""
    if state.tool_call_id != task.wire_call_id:
        raise TaskGateStateError("task-gate call id does not match its task row")
    _validate_approval_original_call(state, expected_tool_name=task.tool_name)


def resolution_receipts_equal(
    left: TaskGateResolutionReceipt,
    right: TaskGateResolutionReceipt,
) -> bool:
    """Receipt equality under storage's JSON rule: bool differs from int, 1 equals 1.0."""
    return (
        left.schema_version == right.schema_version
        and left.occurrence_token == right.occurrence_token
        and left.gate_seq == right.gate_seq
        and left.event_key == right.event_key
        and json_equal(left.payload, right.payload)
    )


def _validate_approval_original_call(
    state: TaskGateState,
    *,
    expected_tool_name: str | None = None,
) -> None:
    if state.yielded.tool_name != "_approval":
        return
    original = state.yielded.resume_metadata.get("original_call")
    if not isinstance(original, dict) or set(original) != {"id", "name", "arguments"}:
        raise TaskGateStateError("approval task gate requires an exact original_call")
    if (
        original["id"] != state.tool_call_id
        or not isinstance(original["name"], str)
        or not original["name"]
        or (expected_tool_name is not None and original["name"] != expected_tool_name)
        or not isinstance(original["arguments"], dict)
        or not json_equal(original["arguments"], state.tool_arguments)
    ):
        raise TaskGateStateError("approval original_call does not match the claimed task")


def _receipt_matches_state(
    receipt: TaskGateResolutionReceipt, state: TaskGateState,
) -> None:
    if (
        receipt.occurrence_token != state.occurrence_token
        or receipt.gate_seq != state.gate_seq
        or receipt.event_key != state.event_key
    ):
        raise TaskGateStateError("task-gate resolution does not match its occurrence")


def _yielded(raw: Any) -> Yielded:
    data = _mapping(raw, "task-gate yielded")
    _exact_keys(
        data,
        {"tool_name", "event_key", "timeout", "resume_metadata", "event_keys"},
        "task-gate yielded",
    )
    timeout = data["timeout"]
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise TaskGateStateError("task-gate yielded timeout must be a number or null")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise TaskGateStateError("task-gate yielded timeout must be finite and non-negative")
    event_keys = data["event_keys"]
    if event_keys is not None:
        if not isinstance(event_keys, list) or not all(
            isinstance(key, str) and key for key in event_keys
        ):
            raise TaskGateStateError("task-gate yielded event_keys must be non-empty strings")
        event_keys = copy.deepcopy(event_keys)
    return Yielded(
        tool_name=_nonempty_text(data["tool_name"], "yielded tool_name"),
        event_key=_nonempty_text(data["event_key"], "yielded event_key"),
        timeout=timeout,
        resume_metadata=copy.deepcopy(_mapping(data["resume_metadata"], "yielded resume_metadata")),
        event_keys=event_keys,
    )


def _copy_yielded(yielded: Yielded) -> Yielded:
    return _yielded({
        "tool_name": yielded.tool_name,
        "event_key": yielded.event_key,
        "timeout": yielded.timeout,
        "resume_metadata": yielded.resume_metadata,
        "event_keys": yielded.event_keys,
    })


def _mapping(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TaskGateStateError(f"{label} must be an object")
    return raw


def _exact_keys(data: dict[str, Any], expected: set[str], label: str) -> None:
    if set(data) != expected:
        missing = sorted(expected - set(data))
        unknown = sorted(set(data) - expected)
        raise TaskGateStateError(
            f"{label} fields are invalid; missing={missing}, unknown={unknown}"
        )


def _token(raw: Any) -> str:
    if not isinstance(raw, str) or _TOKEN_RE.fullmatch(raw) is None:
        raise TaskGateStateError("task-gate occurrence token must be 32 lowercase hex characters")
    return raw


def _integer(raw: Any, label: str, *, minimum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise TaskGateStateError(f"{label} must be an integer >= {minimum}")
    return raw


def _nonempty_text(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise TaskGateStateError(f"{label} must be a non-empty string")
    return raw


def _aware_datetime(raw: Any, label: str) -> datetime:
    if not isinstance(raw, str):
        raise TaskGateStateError(f"{label} must be an ISO-8601 string")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise TaskGateStateError(f"{label} must be an ISO-8601 string") from exc
    return _ensure_aware(value, label)


def _ensure_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TaskGateStateError(f"{label} must carry a timezone")
    return value


__all__ = [
    "LegacyApprovalTaskGateState",
    "TASK_GATE_RESOLUTION_VERSION",
    "TASK_GATE_STATE_VERSION",
    "TaskGateDecisionClass",
    "TaskGateResolutionReceipt",
    "TaskGateState",
    "TaskGateStateError",
    "classify_task_gate_decision",
    "decode_task_gate_state",
    "make_resolution_receipt",
    "new_task_gate_state",
    "resolution_receipts_equal",
    "validate_task_gate_call",
]
