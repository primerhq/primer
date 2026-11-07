"""Bounded repair for dormant staged task-scoped ``ask_user`` decisions."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic_core import to_jsonable_python

import primer.observability.metrics as _metrics
from primer.claim.task_ask_resume import (
    TaskAskResumeOutcome,
    project_task_ask_responder,
    promote_task_ask_resumable,
)
from primer.int.claim import ClaimKind
from primer.model.chat import ToolResultPart
from primer.model.storage import CursorPage, Op, Predicate
from primer.model.tool_call_task import (
    TaskGateRepairStage,
    ToolCallTask,
    ToolCallTaskState,
    parse_scoped_task_id,
)
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.tool_wait_batches import batches_referenced_by_park
from primer.storage import raw_generation
from primer.storage.q import Q
from primer.worker.task_gate_runtime import (
    TaskGateState,
    TaskGateStateError,
    decode_task_gate_state,
)

if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
    from primer.int.storage import Storage
    from primer.int.storage_provider import StorageProvider

logger = logging.getLogger(__name__)

Source = Literal["boot", "periodic"]
Outcome = Literal[
    "promoted",
    "rearmed",
    "lease_present",
    "deferred",
    "quarantined",
    "stale",
    "missing",
    "error",
]
TASK_ASK_REPAIR_PAGE_SIZE = 200
_QUARANTINE_ERROR = "task ask decision could not be repaired"


class _Disposition(StrEnum):
    LIVE = "live"
    DEFER = "defer"
    DEAD = "dead"
    CORRUPT = "corrupt"


def _finish(source: Source, outcome: Outcome) -> Outcome:
    _metrics.task_ask_repair_outcomes_total.labels(source, outcome).inc()
    return outcome


def _candidate_query():
    gated = (
        Q(ToolCallTask)
        .where("state", ToolCallTaskState.GATED.value)
        .where_in("repair_stage", [
            TaskGateRepairStage.RECEIPT_STAGED.value,
            TaskGateRepairStage.PROJECTED.value,
        ])
    )
    promoted = (
        Q(ToolCallTask)
        .where("state", ToolCallTaskState.QUEUED.value)
        .where("repair_stage", TaskGateRepairStage.PROMOTED.value)
    )
    return Predicate(
        left=Q(ToolCallTask).where_null("claim_token").build(),
        op=Op.AND,
        right=Q.or_(gated, promoted).build(),
    )


def _repair_where(task: ToolCallTask) -> dict[str, list[object]]:
    fields = (
        "state",
        "session_id",
        "turn_no",
        "gate_occurrence_token",
        "gate_seq",
        "gate_event_key",
        "gate_until",
        "claim_token",
        "gate_state_sha256",
        "repair_stage",
        "attempts",
        "started_at",
        "finished_at",
    )
    return {field: [raw_generation(task, field)] for field in fields}


def _classify_candidate(
    task: ToolCallTask,
    session: WorkspaceSession | None,
) -> tuple[_Disposition, TaskGateState | None]:
    if task.claim_token is not None:
        return _Disposition.DEFER, None
    try:
        gate = decode_task_gate_state(task.gate_state, task=task)
        scoped = parse_scoped_task_id(task.id, task.session_id)
    except (TaskGateStateError, ValueError):
        return _Disposition.CORRUPT, None
    receipt = gate.resolution if isinstance(gate, TaskGateState) else None
    responder = receipt.responder if receipt is not None else None
    if (
        not isinstance(gate, TaskGateState)
        or gate.yielded.tool_name != "ask_user"
        or receipt is None
        or receipt.schema_version != 2
        or responder is None
        or scoped.node != "x"
        or scoped.turn_no != task.turn_no
    ):
        return _Disposition.CORRUPT, None
    if session is None:
        return _Disposition.DEAD, gate
    status = getattr(session.status, "value", session.status)
    if (
        status == SessionStatus.ENDED.value
        or session.cancel_requested
        or session.id != task.session_id
        or session.parked_status not in {"parked", "resumable"}
        or session.turn_no != task.turn_no
        or session.binding_epoch != gate.binding_epoch
        or session.responder_epoch != responder.responder_epoch
    ):
        return _Disposition.DEAD, gate
    if not any(
        task.id in ref.outstanding_ids
        for ref in batches_referenced_by_park(session.parked_state).values()
    ):
        return _Disposition.DEAD, gate
    if (
        status == SessionStatus.PAUSED.value
        or session.pause_requested
        or session.interrupt_requested
    ):
        return _Disposition.DEFER, gate
    if status not in {
        SessionStatus.CREATED.value,
        SessionStatus.RUNNING.value,
        SessionStatus.WAITING.value,
    }:
        return _Disposition.DEAD, gate
    return _Disposition.LIVE, gate


async def _hold_transient(
    task: ToolCallTask,
    *,
    task_storage: "Storage[ToolCallTask]",
    engine: "ClaimEngine",
    source: Source,
) -> Outcome:
    """Make a promoted task nonclaimable before removing its lease."""
    if (
        task.state is ToolCallTaskState.GATED
        and task.repair_stage in {
            TaskGateRepairStage.RECEIPT_STAGED,
            TaskGateRepairStage.PROJECTED,
        }
        and task.claim_token is None
    ):
        return _finish(source, "deferred")
    if not (
        task.state is ToolCallTaskState.QUEUED
        and task.repair_stage is TaskGateRepairStage.PROMOTED
        and task.claim_token is None
    ):
        return _finish(source, "stale")
    held = await task_storage.patch_if(
        task.id,
        {
            "state": ToolCallTaskState.GATED.value,
            "repair_stage": TaskGateRepairStage.PROJECTED.value,
        },
        where=_repair_where(task),
    )
    if held is None:
        return _finish(source, "stale")
    await engine.delete_lease(ClaimKind.TOOL_CALL, task.id)
    return _finish(source, "deferred")


async def _quarantine(
    task: ToolCallTask,
    *,
    task_storage: "Storage[ToolCallTask]",
    engine: "ClaimEngine",
    source: Source,
    session_storage: "Storage[WorkspaceSession]",
) -> Outcome:
    patch = to_jsonable_python({
        "state": ToolCallTaskState.FAILED.value,
        "finished_at": datetime.now(timezone.utc),
        "last_error": _QUARANTINE_ERROR,
        "result_state": ToolResultPart(
            id=task.wire_call_id,
            output=_QUARANTINE_ERROR,
            error=True,
        ).model_dump(mode="json"),
        "gate_event_key": None,
        "gate_until": None,
        "gate_state": None,
        "gate_state_sha256": None,
        "repair_stage": TaskGateRepairStage.NONE.value,
        "gate_occurrence_token": None,
        "gate_resolution": None,
        "claim_token": None,
    })
    updated = await task_storage.patch_if(
        task.id, patch, where=_repair_where(task),
    )
    if updated is None:
        return _finish(source, "stale")
    await engine.delete_lease(ClaimKind.TOOL_CALL, task.id)
    logger.warning(
        "task ask repair quarantined task %s for session %s",
        task.id,
        task.session_id,
    )
    try:
        from primer.claim.tool_wait_reconcile import (
            verify_and_mark_batch_resumable,
        )

        await verify_and_mark_batch_resumable(
            session_id=task.session_id,
            session_storage=session_storage,
            task_storage=task_storage,
            engine=engine,
            source=source,
        )
    except Exception:
        logger.exception(
            "task ask quarantine could not verify terminal siblings for task %s",
            task.id,
        )
    return _finish(source, "quarantined")


async def _converge_after_promotion(
    task_id: str,
    *,
    task_storage: "Storage[ToolCallTask]",
    session_storage: "Storage[WorkspaceSession]",
    engine: "ClaimEngine",
    source: Source,
    success_outcome: Literal["promoted", "rearmed"],
) -> Outcome:
    """Fresh-read after C and close any lifecycle race before returning."""
    task = await task_storage.get(task_id)
    if task is None:
        return _finish(source, "missing")
    if task.state is ToolCallTaskState.RUNNING or task.claim_token is not None:
        return _finish(source, "stale")
    session = await session_storage.get(task.session_id)
    disposition, _ = _classify_candidate(task, session)
    if disposition is _Disposition.DEFER:
        return await _hold_transient(
            task, task_storage=task_storage, engine=engine, source=source,
        )
    if disposition in {_Disposition.DEAD, _Disposition.CORRUPT}:
        return await _quarantine(
            task,
            task_storage=task_storage,
            session_storage=session_storage,
            engine=engine,
            source=source,
        )
    if (
        task.state is ToolCallTaskState.QUEUED
        and task.repair_stage is TaskGateRepairStage.PROMOTED
        and task.claim_token is None
    ):
        return _finish(source, success_outcome)
    return _finish(source, "stale")


async def repair_task_ask_decision(
    task_id: str,
    *,
    task_storage: "Storage[ToolCallTask]",
    session_storage: "Storage[WorkspaceSession]",
    engine: "ClaimEngine",
    source: Source,
) -> Outcome:
    """Repair or quarantine one exact staged decision without external effects."""
    for _attempt in range(2):
        task = await task_storage.get(task_id)
        if task is None:
            return _finish(source, "missing")
        if task.state is ToolCallTaskState.RUNNING or task.claim_token is not None:
            return _finish(source, "stale")
        session = await session_storage.get(task.session_id)
        disposition, gate = _classify_candidate(task, session)
        expected = {
            TaskGateRepairStage.RECEIPT_STAGED: ToolCallTaskState.GATED,
            TaskGateRepairStage.PROJECTED: ToolCallTaskState.GATED,
            TaskGateRepairStage.PROMOTED: ToolCallTaskState.QUEUED,
        }.get(task.repair_stage)
        if disposition is _Disposition.DEFER:
            return await _hold_transient(
                task, task_storage=task_storage, engine=engine, source=source,
            )
        if disposition in {_Disposition.DEAD, _Disposition.CORRUPT} or (
            gate is None or expected is not task.state
        ):
            return await _quarantine(
                task,
                task_storage=task_storage,
                session_storage=session_storage,
                engine=engine,
                source=source,
            )

        if task.repair_stage in {
            TaskGateRepairStage.RECEIPT_STAGED,
            TaskGateRepairStage.PROJECTED,
        }:
            outcome = await project_task_ask_responder(
                task.id,
                task_storage=task_storage,
                session_storage=session_storage,
                engine=engine,
            )
            if outcome in {
                TaskAskResumeOutcome.REFUSED_CORRUPT,
                TaskAskResumeOutcome.REFUSED_CONFLICT,
            }:
                fresh = await task_storage.get(task.id)
                if fresh is None:
                    return _finish(source, "missing")
                return await _quarantine(
                    fresh,
                    task_storage=task_storage,
                    session_storage=session_storage,
                    engine=engine,
                    source=source,
                )
            if outcome is TaskAskResumeOutcome.REFUSED_MISSING:
                return _finish(source, "missing")
            if outcome not in {
                TaskAskResumeOutcome.PROJECTED,
                TaskAskResumeOutcome.PROMOTED,
            }:
                continue

            # B can await and can repair the session projection. C is allowed
            # only after a fresh task/session read repeats the full proof.
            fresh_task = await task_storage.get(task.id)
            if fresh_task is None:
                return _finish(source, "missing")
            fresh_session = await session_storage.get(fresh_task.session_id)
            fresh_disposition, _fresh_gate = _classify_candidate(
                fresh_task, fresh_session,
            )
            if fresh_disposition is _Disposition.DEFER:
                return await _hold_transient(
                    fresh_task,
                    task_storage=task_storage,
                    engine=engine,
                    source=source,
                )
            if fresh_disposition is not _Disposition.LIVE:
                return await _quarantine(
                    fresh_task,
                    task_storage=task_storage,
                    session_storage=session_storage,
                    engine=engine,
                    source=source,
                )
            if fresh_task.repair_stage is not TaskGateRepairStage.PROJECTED:
                continue
            outcome = await promote_task_ask_resumable(
                fresh_task.id, task_storage=task_storage, engine=engine,
            )
            if outcome is TaskAskResumeOutcome.PROMOTED:
                return await _converge_after_promotion(
                    fresh_task.id,
                    task_storage=task_storage,
                    session_storage=session_storage,
                    engine=engine,
                    source=source,
                    success_outcome="promoted",
                )
            if outcome is TaskAskResumeOutcome.REFUSED_MISSING:
                return _finish(source, "missing")
            continue

        if await engine.has_lease(ClaimKind.TOOL_CALL, task.id):
            return _finish(source, "lease_present")
        outcome = await promote_task_ask_resumable(
            task.id, task_storage=task_storage, engine=engine,
        )
        if outcome is TaskAskResumeOutcome.PROMOTED:
            return await _converge_after_promotion(
                task.id,
                task_storage=task_storage,
                session_storage=session_storage,
                engine=engine,
                source=source,
                success_outcome="rearmed",
            )
        if outcome is TaskAskResumeOutcome.REFUSED_MISSING:
            return _finish(source, "missing")
    return _finish(source, "stale")


async def reconcile_staged_task_asks(
    storage_provider: "StorageProvider",
    engine: "ClaimEngine",
    *,
    source: Source,
    cursor: str | None = None,
) -> tuple[str | None, int]:
    """Process one bounded keyset page and return its continuation cursor."""
    task_storage = storage_provider.get_storage(ToolCallTask)
    session_storage = storage_provider.get_storage(WorkspaceSession)
    page = await task_storage.find(
        _candidate_query(),
        CursorPage(cursor=cursor, length=TASK_ASK_REPAIR_PAGE_SIZE),
    )
    for task in page.items:
        try:
            await repair_task_ask_decision(
                task.id,
                task_storage=task_storage,
                session_storage=session_storage,
                engine=engine,
                source=source,
            )
        except Exception:
            _metrics.task_ask_repair_outcomes_total.labels(source, "error").inc()
            logger.exception("task ask repair failed for task %s", task.id)
    _metrics.task_ask_repair_candidates_total.labels(source).inc(len(page.items))
    return page.next_cursor, len(page.items)


__all__ = [
    "TASK_ASK_REPAIR_PAGE_SIZE",
    "reconcile_staged_task_asks",
    "repair_task_ask_decision",
]
