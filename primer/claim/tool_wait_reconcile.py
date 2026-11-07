"""Exact repair of terminal tool-call batches whose post-release wake was lost."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import primer.observability.metrics as _metrics
from primer.int.claim import ClaimKind, PostReleaseWake
from primer.model.storage import CursorPage
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.responder_attribution import ReservationFences
from primer.session.tool_wait_batches import (
    strict_batches_referenced_by_park,
    task_is_live_for_session,
)
from primer.session.yields import (
    durably_mark_session_resumable,
    tool_wait_event_key_or_none,
)
from primer.storage import raw_generation
from primer.storage.q import Q

if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
    from primer.int.storage import Storage
    from primer.int.storage_provider import StorageProvider

logger = logging.getLogger(__name__)

Source = Literal["hook", "boot", "periodic"]
Detail = Literal[
    "advanced", "lease_rearmed", "stale", "missing", "not_ready",
    "lifecycle", "corrupt", "grace", "lease_present",
]
MetricOutcome = Literal[
    "woken", "rearmed", "not_ready", "malformed", "stale", "missing", "error",
]
RECONCILE_PAGE_SIZE = 200
RESUMABLE_REARM_GRACE = timedelta(seconds=30)
_PAYLOAD = {"tool_wait_ready": True}
_METRIC_OUTCOME: dict[Detail, MetricOutcome] = {
    "advanced": "woken",
    "lease_rearmed": "rearmed",
    "lease_present": "not_ready",
    "grace": "not_ready",
    "not_ready": "not_ready",
    "stale": "stale",
    "lifecycle": "stale",
    "corrupt": "malformed",
    "missing": "missing",
}


def _terminal_tool_wait_candidate_predicate():
    """Portable selector for the bounded marker-backed reconciliation scan."""
    return (
        Q(WorkspaceSession)
        .where_not_null("parked_tool_batches")
        .where_in("parked_status", ["parked", "resumable"])
        .build()
    )


def _finish(source: Source, detail: Detail) -> Detail:
    """Count one attempt using the closed low-cardinality outcome vocabulary."""
    _metrics.tool_wait_reconcile_outcomes_total.labels(
        source, _METRIC_OUTCOME[detail],
    ).inc()
    return detail


def _current_event_keys(session: WorkspaceSession) -> set[str]:
    keys = set(session.parked_event_keys or ())
    if session.parked_event_key:
        keys.add(session.parked_event_key)
    return keys


def _occurrence_where(session: WorkspaceSession) -> dict[str, list[object]]:
    where: dict[str, list[object]] = dict(ReservationFences.from_session(session).where())
    # parked_status is the one field the wake intentionally advances and
    # parked_at keeps its legacy equivalent-spelling support in yields.py.
    where.pop("parked_status", None)
    where.pop("parked_at", None)
    where["parked_tool_batches"] = [raw_generation(session, "parked_tool_batches")]
    return where


async def _verify_and_mark_batch_resumable(
    *,
    session_id: str,
    session_storage: "Storage[WorkspaceSession]",
    task_storage: "Storage[ToolCallTask]",
    engine: "ClaimEngine",
    source: Source,
    signal: PostReleaseWake | None = None,
    now: datetime | None = None,
) -> Detail:
    """Verify one current batch exactly, then apply the ordinary durable wake."""

    session = await session_storage.get(session_id)
    if session is None:
        return _finish(source, "missing")
    marker_is_legacy_hook = (
        source == "hook"
        and signal is not None
        and session.parked_tool_batches is None
    )
    if (
        (session.parked_tool_batches is not True and not marker_is_legacy_hook)
        or session.parked_status not in {"parked", "resumable"}
        or session.status in {SessionStatus.ENDED, SessionStatus.PAUSED}
        or session.pause_requested
        or session.cancel_requested
        or session.interrupt_requested
        or session.parked_at is None
    ):
        return _finish(source, "lifecycle")

    refs = strict_batches_referenced_by_park(session.parked_state)
    event_keys = _current_event_keys(session)
    if (not refs and refs.malformed_count == 0) or not event_keys:
        return _finish(source, "corrupt")
    valid_refs = [ref for ref in refs.values() if ref.well_formed]
    malformed_count = len(refs) - len(valid_refs) + refs.malformed_count
    if malformed_count:
        logger.warning(
            "tool-wait reconciliation ignored %d malformed batch proof(s) for session %s",
            malformed_count, session.id,
        )
    if not valid_refs:
        return _finish(source, "corrupt")

    for ref in valid_refs:
        task_ids = ref.task_ids
        event_key = tool_wait_event_key_or_none(
            session.id, scoped_task_id=task_ids[0], site="reconciler",
        )
        if event_key is None or event_key not in event_keys:
            continue
        if signal is not None and (
            event_key != signal.event_key
            or task_ids != signal.required_terminal_task_ids
        ):
            continue
        tasks: list[ToolCallTask] = []
        valid = True
        for task_id in task_ids:
            task = await task_storage.get(task_id)
            if (
                task is None
                or task.session_id != session.id
                or task.turn_no != session.turn_no
                or task.state not in {ToolCallTaskState.DONE, ToolCallTaskState.FAILED}
                or tuple(task.batch_task_ids) != task_ids
                or not task_is_live_for_session(task, session)[0]
            ):
                valid = False
                break
            tasks.append(task)
        if not valid or len(tasks) != len(task_ids):
            continue

        if session.parked_status == "resumable" and source in {"boot", "periodic"}:
            current = now or datetime.now(timezone.utc)
            if session.resumable_at is None or current - session.resumable_at < RESUMABLE_REARM_GRACE:
                return _finish(source, "grace")
            if await engine.has_lease(ClaimKind.SESSION, session.id):
                return _finish(source, "lease_present")

        applied = await durably_mark_session_resumable(
            session,
            event_key=event_key,
            payload=_PAYLOAD,
            session_storage=session_storage,
            engine=engine,
            _occurrence_where=_occurrence_where(session),
        )
        detail: Detail = "advanced" if applied and session.parked_status == "parked" else (
            "lease_rearmed" if applied else "stale"
        )
        return _finish(source, detail)
    return _finish(source, "not_ready")


async def verify_and_mark_batch_resumable(
    *,
    session_id: str,
    session_storage: "Storage[WorkspaceSession]",
    task_storage: "Storage[ToolCallTask]",
    engine: "ClaimEngine",
    source: Source,
    signal: PostReleaseWake | None = None,
    now: datetime | None = None,
) -> Detail:
    """Count every hook or repair attempt once, including unexpected errors."""
    try:
        return await _verify_and_mark_batch_resumable(
            session_id=session_id,
            session_storage=session_storage,
            task_storage=task_storage,
            engine=engine,
            source=source,
            signal=signal,
            now=now,
        )
    except Exception:
        _metrics.tool_wait_reconcile_outcomes_total.labels(source, "error").inc()
        raise


async def reconcile_terminal_tool_wait_parks(
    storage_provider: "StorageProvider",
    engine: "ClaimEngine",
    *,
    source: Literal["boot", "periodic"],
    cursor: str | None = None,
) -> tuple[str | None, int]:
    """Process one bounded keyset page and return its continuation cursor."""

    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)
    predicate = _terminal_tool_wait_candidate_predicate()
    page = await session_storage.find(
        predicate, CursorPage(cursor=cursor, length=RECONCILE_PAGE_SIZE),
    )
    for session in page.items:
        outcome = await verify_and_mark_batch_resumable(
            session_id=session.id,
            session_storage=session_storage,
            task_storage=task_storage,
            engine=engine,
            source=source,
        )
        if outcome == "corrupt":
            logger.warning(
                "tool-wait reconciliation refused corrupt batch proof for session %s",
                session.id,
            )
    _metrics.tool_wait_reconcile_candidates_total.labels(source).inc(len(page.items))
    return page.next_cursor, len(page.items)


__all__ = [
    "RECONCILE_PAGE_SIZE",
    "RESUMABLE_REARM_GRACE",
    "reconcile_terminal_tool_wait_parks",
    "verify_and_mark_batch_resumable",
]
