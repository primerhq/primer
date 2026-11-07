"""Validated lookup of live task-scoped human gates.

This module has no HTTP dependencies.  Decision surfaces use it to resolve a
public occurrence token to one durable ``ToolCallTask`` without re-deriving the
task/session liveness or v1 envelope checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from primer.model.except_ import PrimerError
from primer.model.storage import OffsetPage, OrderBy
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.session.tool_wait_batches import batches_referenced_by_park
from primer.storage.q import Q
from primer.worker.task_gate_runtime import (
    TaskGateState,
    TaskGateStateError,
    decode_task_gate_state,
)


@dataclass(frozen=True)
class TaskApprovalGate:
    """One validated approval occurrence and its scheduling row."""

    task: ToolCallTask
    gate: TaskGateState


class TaskApprovalLookupKind(StrEnum):
    """Public-decision classification without HTTP policy."""

    MATCH = "match"
    ABSENT = "absent"
    MOVED = "moved"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class TaskApprovalLookup:
    kind: TaskApprovalLookupKind
    match: TaskApprovalGate | None = None


def task_approval_park_present(session) -> bool:
    """Whether this session park names any independently claimed task."""
    return any(
        ref.outstanding_ids
        for ref in batches_referenced_by_park(
            getattr(session, "parked_state", None),
        ).values()
    )


def _live_outstanding_ids(session) -> set[str]:
    status = getattr(session, "status", None)
    if (
        getattr(status, "value", status) not in {"created", "running", "waiting"}
        or getattr(session, "parked_status", None) != "parked"
        or getattr(session, "pause_requested", False)
        or getattr(session, "cancel_requested", False)
    ):
        return set()
    return {
        task_id
        for ref in batches_referenced_by_park(
            getattr(session, "parked_state", None),
        ).values()
        for task_id in ref.outstanding_ids
    }


def _validate_task_approval(
    task: ToolCallTask,
    session,
    *,
    pending: bool,
) -> TaskApprovalGate:
    if task.session_id != session.id or task.id not in _live_outstanding_ids(session):
        raise TaskGateStateError(
            "task-gate approval is not referenced by the session's live park"
        )
    gate = decode_task_gate_state(task.gate_state, task=task)
    if not isinstance(gate, TaskGateState) or gate.yielded.tool_name != "_approval":
        raise TaskGateStateError("task gate is not a v1 approval")
    if gate.tool_call_id != task.wire_call_id:
        raise TaskGateStateError("task-gate approval call identity does not match")
    if gate.binding_epoch != session.binding_epoch:
        raise TaskGateStateError("task-gate approval binding epoch is stale")
    if pending and (
        task.state is not ToolCallTaskState.GATED
        or task.claim_token is not None
        or gate.resolution is not None
        or task.gate_resolution is not None
    ):
        raise TaskGateStateError("task-gate approval is not pending")
    return TaskApprovalGate(task=task, gate=gate)


async def pending_task_approvals(task_storage, session) -> list[TaskApprovalGate]:
    """Return the session's valid unresolved task approvals deterministically."""
    page = await task_storage.find(
        Q(ToolCallTask)
        .where("session_id", session.id)
        .where("state", ToolCallTaskState.GATED.value)
        .build(),
        OffsetPage(offset=0, length=200),
        order_by=[OrderBy(field="created_at", direction="asc")],
    )
    found: list[TaskApprovalGate] = []
    for task in page.items:
        try:
            found.append(_validate_task_approval(task, session, pending=True))
        except TaskGateStateError:
            continue
    return found


async def exact_task_approval(
    task_storage,
    session,
    *,
    occurrence_token: str,
) -> TaskApprovalLookup:
    """Classify one exact occurrence across its decision/claim window."""
    page = await task_storage.find(
        Q(ToolCallTask)
        .where_in("state", [
            ToolCallTaskState.GATED.value,
            ToolCallTaskState.QUEUED.value,
            ToolCallTaskState.RUNNING.value,
            ToolCallTaskState.DONE.value,
            ToolCallTaskState.FAILED.value,
        ])
        .where("session_id", session.id)
        .where("gate_occurrence_token", occurrence_token)
        .build(),
        OffsetPage(offset=0, length=2),
    )
    if len(page.items) > 1:
        raise PrimerError(
            "multiple live tool-call tasks carry one gate occurrence token"
        )
    if not page.items:
        return TaskApprovalLookup(TaskApprovalLookupKind.ABSENT)
    try:
        match = _validate_task_approval(page.items[0], session, pending=False)
    except TaskGateStateError:
        task = page.items[0]
        try:
            decode_task_gate_state(task.gate_state, task=task)
        except TaskGateStateError:
            return TaskApprovalLookup(TaskApprovalLookupKind.CORRUPT)
        return TaskApprovalLookup(TaskApprovalLookupKind.MOVED)
    return TaskApprovalLookup(TaskApprovalLookupKind.MATCH, match)


__all__ = [
    "TaskApprovalGate",
    "TaskApprovalLookup",
    "TaskApprovalLookupKind",
    "exact_task_approval",
    "pending_task_approvals",
    "task_approval_park_present",
]
