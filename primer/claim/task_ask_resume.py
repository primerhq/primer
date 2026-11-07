"""Dormant staged persistence for task-scoped ``ask_user`` decisions.

The three primitives deliberately stop at durable storage boundaries. No
router calls them while human task gates are hard-disabled. The task receipt
is staged first, responder authority is then reserved and projected on the
session, and only that durable projection permits task promotion and lease
re-arm.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from primer.int.claim import ClaimKind
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.tool_call_task import (
    TaskGateRepairStage,
    ToolCallTask,
    ToolCallTaskState,
)
from primer.session.responder_attribution import (
    ReservationFences,
    project_reservation,
    reserve_canonical_source,
)
from primer.session.tool_wait_batches import batches_referenced_by_park
from primer.storage import raw_generation
from primer.storage._patch import json_equal
from primer.storage.cas import patch_if_or_current
from primer.worker.task_gate_runtime import (
    TaskGateResolutionReceipt,
    TaskGateState,
    TaskGateStateError,
    decode_task_gate_state,
    gate_state_sha256,
    make_task_ask_resolution_receipt,
)

if TYPE_CHECKING:
    from datetime import datetime

    from primer.int.claim import ClaimEngine
    from primer.int.storage import Storage
    from primer.model.workspace_session import WorkspaceSession


class TaskAskResumeOutcome(StrEnum):
    """Result of one dormant staged-repair step."""

    RECEIPT_STAGED = "receipt_staged"
    PROJECTED = "projected"
    PROMOTED = "promoted"
    REFUSED_MISSING = "refused_missing"
    REFUSED_STALE = "refused_stale"
    REFUSED_CONFLICT = "refused_conflict"
    REFUSED_CORRUPT = "refused_corrupt"
    REFUSED_SESSION = "refused_session"


async def stage_task_ask_receipt(
    task_id: str,
    *,
    occurrence_token: str,
    gate_seq: int,
    event_key: str,
    payload: dict[str, Any] | None,
    person: PrincipalRef,
    responder_epoch: str,
    accepted_at: "datetime",
    task_storage: "Storage[ToolCallTask]",
    engine: "ClaimEngine",
) -> TaskAskResumeOutcome:
    """Stage one canonical v2 receipt on an exact unclaimed GATED row."""
    _require_engine(engine)
    task = await task_storage.get(task_id)
    if task is None:
        return TaskAskResumeOutcome.REFUSED_MISSING
    if not _exact_occurrence(task, occurrence_token, gate_seq, event_key):
        return TaskAskResumeOutcome.REFUSED_STALE
    gate = _decode_exact_ask(task)
    if gate is None:
        return TaskAskResumeOutcome.REFUSED_CORRUPT
    try:
        proposed = make_task_ask_resolution_receipt(
            state=gate,
            task=task,
            payload=payload,
            person=person,
            responder_epoch=responder_epoch,
            accepted_at=accepted_at,
        )
    except TaskGateStateError:
        return TaskAskResumeOutcome.REFUSED_CORRUPT

    existing = gate.resolution
    if existing is not None:
        return _classify_existing(task, existing, proposed)
    if (
        task.state is not ToolCallTaskState.GATED
        or task.claim_token is not None
        or task.gate_resolution is not None
        or task.repair_stage is not TaskGateRepairStage.NONE
    ):
        return TaskAskResumeOutcome.REFUSED_STALE

    resolved = gate.with_resolution(proposed).to_jsonable()
    patch = {
        "gate_state": resolved,
        "gate_state_sha256": gate_state_sha256(resolved),
        "gate_resolution": proposed.to_jsonable(),
        "repair_stage": TaskGateRepairStage.RECEIPT_STAGED.value,
    }
    where = _task_where(task, stage=TaskGateRepairStage.NONE)
    where["gate_resolution"] = [None]
    try:
        written, current = await patch_if_or_current(
            task_storage, task.id, patch, where=where,
        )
    except NotFoundError:
        return TaskAskResumeOutcome.REFUSED_MISSING
    if written is not None:
        return TaskAskResumeOutcome.RECEIPT_STAGED
    if current is None:
        return TaskAskResumeOutcome.REFUSED_MISSING
    current_gate = _decode_exact_ask(current)
    if current_gate is None or current_gate.resolution is None:
        return TaskAskResumeOutcome.REFUSED_STALE
    return _classify_existing(current, current_gate.resolution, proposed)


async def project_task_ask_responder(
    task_id: str,
    *,
    task_storage: "Storage[ToolCallTask]",
    session_storage: "Storage[WorkspaceSession]",
    engine: "ClaimEngine",
) -> TaskAskResumeOutcome:
    """Reserve/project the staged responder, then advance to PROJECTED."""
    _require_engine(engine)
    task = await task_storage.get(task_id)
    if task is None:
        return TaskAskResumeOutcome.REFUSED_MISSING
    gate = _decode_exact_ask(task)
    if gate is None or gate.resolution is None:
        return TaskAskResumeOutcome.REFUSED_CORRUPT
    receipt = gate.resolution
    if task.repair_stage is TaskGateRepairStage.PROMOTED:
        return (
            TaskAskResumeOutcome.PROMOTED
            if task.state is ToolCallTaskState.QUEUED and task.claim_token is None
            else TaskAskResumeOutcome.REFUSED_STALE
        )
    if task.repair_stage not in {
        TaskGateRepairStage.RECEIPT_STAGED,
        TaskGateRepairStage.PROJECTED,
    } or task.state is not ToolCallTaskState.GATED or task.claim_token is not None:
        return TaskAskResumeOutcome.REFUSED_STALE
    responder = receipt.responder
    if responder is None:
        return TaskAskResumeOutcome.REFUSED_CORRUPT

    session = await session_storage.get(task.session_id)
    if session is None:
        return TaskAskResumeOutcome.REFUSED_SESSION
    if not _session_is_live_for_gate(session, task, gate, responder.responder_epoch):
        return TaskAskResumeOutcome.REFUSED_SESSION
    fences = ReservationFences.from_session(session)
    try:
        reserved = await reserve_canonical_source(
            session_storage,
            session.id,
            responder_epoch=responder.responder_epoch,
            person=responder.person,
            domain=responder.domain,
            source_identity=responder.source_identity,
            source_key=responder.source_key,
            accepted_at=responder.accepted_at,
            fences=fences,
        )
        if reserved.reservation.accepted_at != responder.accepted_at:
            return TaskAskResumeOutcome.REFUSED_SESSION
        await project_reservation(
            session_storage,
            session.id,
            responder_epoch=responder.responder_epoch,
            source_key=responder.source_key,
            fences=fences,
        )
    except (ConflictError, ValueError):
        return TaskAskResumeOutcome.REFUSED_SESSION

    if task.repair_stage is TaskGateRepairStage.PROJECTED:
        return TaskAskResumeOutcome.PROJECTED
    where = _task_where(task, stage=TaskGateRepairStage.RECEIPT_STAGED)
    try:
        written, current = await patch_if_or_current(
            task_storage,
            task.id,
            {"repair_stage": TaskGateRepairStage.PROJECTED.value},
            where=where,
        )
    except NotFoundError:
        return TaskAskResumeOutcome.REFUSED_MISSING
    if written is not None:
        return TaskAskResumeOutcome.PROJECTED
    return _classify_stage_after_race(current, receipt)


async def promote_task_ask_resumable(
    task_id: str,
    *,
    task_storage: "Storage[ToolCallTask]",
    engine: "ClaimEngine",
) -> TaskAskResumeOutcome:
    """Promote one projected task, then idempotently re-arm its lease."""
    _require_engine(engine)
    task = await task_storage.get(task_id)
    if task is None:
        return TaskAskResumeOutcome.REFUSED_MISSING
    gate = _decode_exact_ask(task)
    if gate is None or gate.resolution is None:
        return TaskAskResumeOutcome.REFUSED_CORRUPT
    receipt = gate.resolution
    if (
        task.repair_stage is TaskGateRepairStage.PROMOTED
        and task.state is ToolCallTaskState.QUEUED
        and task.claim_token is None
    ):
        await engine.mark_resumable(ClaimKind.TOOL_CALL, task.id)
        return TaskAskResumeOutcome.PROMOTED
    if (
        task.repair_stage is not TaskGateRepairStage.PROJECTED
        or task.state is not ToolCallTaskState.GATED
        or task.claim_token is not None
    ):
        return TaskAskResumeOutcome.REFUSED_STALE

    where = _task_where(task, stage=TaskGateRepairStage.PROJECTED)
    try:
        written, current = await patch_if_or_current(
            task_storage,
            task.id,
            {
                "state": ToolCallTaskState.QUEUED.value,
                "repair_stage": TaskGateRepairStage.PROMOTED.value,
            },
            where=where,
        )
    except NotFoundError:
        return TaskAskResumeOutcome.REFUSED_MISSING
    if written is None:
        outcome = _classify_stage_after_race(current, receipt)
        if outcome is not TaskAskResumeOutcome.PROMOTED:
            return outcome
        assert current is not None
        written = current
    await engine.mark_resumable(ClaimKind.TOOL_CALL, written.id)
    return TaskAskResumeOutcome.PROMOTED


def _require_engine(engine: "ClaimEngine") -> None:
    if engine is None or not callable(getattr(engine, "mark_resumable", None)):
        raise ValueError("a ClaimEngine is required for a task ask decision")


def _decode_exact_ask(task: ToolCallTask) -> TaskGateState | None:
    if task.gate_state_sha256 is None:
        return None
    try:
        gate = decode_task_gate_state(task.gate_state, task=task)
    except TaskGateStateError:
        return None
    if not isinstance(gate, TaskGateState) or gate.yielded.tool_name != "ask_user":
        return None
    return gate


def _exact_occurrence(
    task: ToolCallTask, occurrence_token: str, gate_seq: int, event_key: str,
) -> bool:
    return (
        task.gate_occurrence_token == occurrence_token
        and task.gate_seq == gate_seq
        and task.gate_event_key == event_key
    )


def _same_retry(
    stored: TaskGateResolutionReceipt,
    proposed: TaskGateResolutionReceipt,
) -> bool:
    left = stored.responder
    right = proposed.responder
    return (
        stored.schema_version == proposed.schema_version == 2
        and stored.occurrence_token == proposed.occurrence_token
        and stored.gate_seq == proposed.gate_seq
        and stored.event_key == proposed.event_key
        and json_equal(stored.payload, proposed.payload)
        and left is not None
        and right is not None
        and left.person == right.person
        and left.responder_epoch == right.responder_epoch
        and left.domain == right.domain
        and left.source_identity == right.source_identity
        and left.source_key == right.source_key
    )


def _classify_existing(
    task: ToolCallTask,
    stored: TaskGateResolutionReceipt,
    proposed: TaskGateResolutionReceipt,
) -> TaskAskResumeOutcome:
    if not _same_retry(stored, proposed):
        return TaskAskResumeOutcome.REFUSED_CONFLICT
    if task.repair_stage in {
        TaskGateRepairStage.RECEIPT_STAGED,
        TaskGateRepairStage.PROJECTED,
    } and (
        task.state is not ToolCallTaskState.GATED or task.claim_token is not None
    ):
        return TaskAskResumeOutcome.REFUSED_STALE
    if task.repair_stage is TaskGateRepairStage.PROMOTED and (
        task.state is not ToolCallTaskState.QUEUED or task.claim_token is not None
    ):
        return TaskAskResumeOutcome.REFUSED_STALE
    return {
        TaskGateRepairStage.RECEIPT_STAGED: TaskAskResumeOutcome.RECEIPT_STAGED,
        TaskGateRepairStage.PROJECTED: TaskAskResumeOutcome.PROJECTED,
        TaskGateRepairStage.PROMOTED: TaskAskResumeOutcome.PROMOTED,
    }.get(task.repair_stage, TaskAskResumeOutcome.REFUSED_CORRUPT)


def _classify_stage_after_race(
    current: ToolCallTask | None,
    expected: TaskGateResolutionReceipt,
) -> TaskAskResumeOutcome:
    if current is None:
        return TaskAskResumeOutcome.REFUSED_MISSING
    gate = _decode_exact_ask(current)
    if gate is None or gate.resolution is None:
        return TaskAskResumeOutcome.REFUSED_CORRUPT
    if not _same_retry(gate.resolution, expected):
        return TaskAskResumeOutcome.REFUSED_CONFLICT
    if current.repair_stage is TaskGateRepairStage.PROJECTED:
        return TaskAskResumeOutcome.PROJECTED
    if (
        current.repair_stage is TaskGateRepairStage.PROMOTED
        and current.state is ToolCallTaskState.QUEUED
        and current.claim_token is None
    ):
        return TaskAskResumeOutcome.PROMOTED
    return TaskAskResumeOutcome.REFUSED_STALE


def _task_where(
    task: ToolCallTask, *, stage: TaskGateRepairStage,
) -> dict[str, list[str | int | bool | None]]:
    fields = (
        "state",
        "session_id",
        "turn_no",
        "gate_occurrence_token",
        "gate_seq",
        "gate_event_key",
        "claim_token",
        "gate_state_sha256",
    )
    where = {field: [raw_generation(task, field)] for field in fields}
    where["repair_stage"] = [stage.value]
    return where


def _session_is_live_for_gate(
    session: "WorkspaceSession",
    task: ToolCallTask,
    gate: TaskGateState,
    responder_epoch: str,
) -> bool:
    status = getattr(session.status, "value", session.status)
    if (
        session.id != task.session_id
        or status not in {"created", "running", "waiting"}
        or session.parked_status not in {"parked", "resumable"}
        or session.turn_no != task.turn_no
        or session.binding_epoch != gate.binding_epoch
        or session.responder_epoch != responder_epoch
        or session.pause_requested
        or session.cancel_requested
        or session.interrupt_requested
    ):
        return False
    return any(
        task.id in ref.outstanding_ids
        for ref in batches_referenced_by_park(session.parked_state).values()
    )


__all__ = [
    "TaskAskResumeOutcome",
    "project_task_ask_responder",
    "promote_task_ask_resumable",
    "stage_task_ask_receipt",
]
