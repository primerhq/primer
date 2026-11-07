"""Occurrence-fenced task-gate decision persistence.

This is the internal, default-off A1 seam. No router, channel, timeout or
executor-resume path calls it yet. It persists one canonical decision receipt
and re-arms the task lease; consuming the receipt is a later slice.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from primer.int.claim import ClaimKind
from primer.model.except_ import NotFoundError
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.storage.cas import patch_if_or_current
from primer.worker.task_gate_runtime import (
    TaskGateDecisionClass,
    TaskGateState,
    TaskGateStateError,
    classify_task_gate_decision,
    decode_task_gate_state,
    make_resolution_receipt,
)

if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
    from primer.int.storage import Storage


class TaskGateResumeOutcome(StrEnum):
    """Typed result of one decision attempt."""

    ADVANCED = "advanced"
    IDEMPOTENT = "idempotent"
    REFUSED_CONFLICT = "refused_conflict"
    REFUSED_STALE = "refused_stale"
    REFUSED_RUNNING = "refused_running"
    REFUSED_TERMINAL = "refused_terminal"
    REFUSED_CORRUPT = "refused_corrupt"
    REFUSED_MISSING = "refused_missing"


_REFUSAL = {
    TaskGateDecisionClass.CONFLICT: TaskGateResumeOutcome.REFUSED_CONFLICT,
    TaskGateDecisionClass.STALE: TaskGateResumeOutcome.REFUSED_STALE,
    TaskGateDecisionClass.RUNNING: TaskGateResumeOutcome.REFUSED_RUNNING,
    TaskGateDecisionClass.TERMINAL: TaskGateResumeOutcome.REFUSED_TERMINAL,
    TaskGateDecisionClass.CORRUPT: TaskGateResumeOutcome.REFUSED_CORRUPT,
}


async def durably_mark_tool_call_task_resumable(
    task_id: str,
    *,
    occurrence_token: str,
    gate_seq: int,
    event_key: str,
    payload: dict[str, Any] | None,
    task_storage: "Storage[ToolCallTask]",
    engine: "ClaimEngine",
) -> TaskGateResumeOutcome:
    """Persist one exact decision and re-arm its task lease.

    The write is one field-scoped compare-and-set fenced on GATED, the exact
    occurrence token, gate sequence and event key, no claim owner, and no
    existing receipt. A same-decision retry on the resulting QUEUED row is
    idempotent and re-runs ``mark_resumable`` so a crash or failure after the
    durable write but before lease re-arm is repairable.
    """
    if engine is None:
        raise ValueError("a ClaimEngine is required to re-arm a decided task gate")
    receipt = make_resolution_receipt(
        occurrence_token=occurrence_token,
        gate_seq=gate_seq,
        event_key=event_key,
        payload=payload,
    )
    task = await task_storage.get(task_id)
    if task is None:
        return TaskGateResumeOutcome.REFUSED_MISSING

    classification = classify_task_gate_decision(task, receipt)
    if classification is TaskGateDecisionClass.IDENTICAL:
        await _rearm_if_wired(task, receipt, engine)
        return TaskGateResumeOutcome.IDEMPOTENT
    if classification is not TaskGateDecisionClass.PENDING:
        return _REFUSAL[classification]

    try:
        gate = decode_task_gate_state(task.gate_state, task=task)
    except TaskGateStateError:
        return TaskGateResumeOutcome.REFUSED_CORRUPT
    if not isinstance(gate, TaskGateState):
        return TaskGateResumeOutcome.REFUSED_CORRUPT
    resolved_gate = gate.with_resolution(receipt)
    patch = {
        "state": ToolCallTaskState.QUEUED.value,
        "gate_state": resolved_gate.to_jsonable(),
        "gate_resolution": receipt.to_jsonable(),
    }
    where = {
        "state": [ToolCallTaskState.GATED.value],
        "gate_occurrence_token": [occurrence_token],
        "gate_seq": [gate_seq],
        "gate_event_key": [event_key],
        "claim_token": [None],
        "gate_resolution": [None],
    }
    try:
        landed, current = await patch_if_or_current(
            task_storage,
            task_id,
            patch,
            where=where,
        )
    except NotFoundError:
        return TaskGateResumeOutcome.REFUSED_MISSING

    if landed is not None:
        # Decode and classify the write itself before arming a lease. A storage
        # implementation that returned a non-canonical row must fail closed.
        if classify_task_gate_decision(landed, receipt) is not TaskGateDecisionClass.IDENTICAL:
            return TaskGateResumeOutcome.REFUSED_CORRUPT
        await _rearm_if_wired(landed, receipt, engine)
        return TaskGateResumeOutcome.ADVANCED

    if current is None:
        return TaskGateResumeOutcome.REFUSED_MISSING
    classification = classify_task_gate_decision(current, receipt)
    if classification is TaskGateDecisionClass.IDENTICAL:
        await _rearm_if_wired(current, receipt, engine)
        return TaskGateResumeOutcome.IDEMPOTENT
    return _REFUSAL.get(classification, TaskGateResumeOutcome.REFUSED_CORRUPT)


async def _rearm_if_wired(
    task: ToolCallTask,
    receipt,
    engine: "ClaimEngine",
) -> None:
    """Re-arm only the exact decided QUEUED occurrence."""
    if classify_task_gate_decision(task, receipt) is not TaskGateDecisionClass.IDENTICAL:
        return
    await engine.mark_resumable(ClaimKind.TOOL_CALL, task.id)


__all__ = [
    "TaskGateResumeOutcome",
    "durably_mark_tool_call_task_resumable",
]
