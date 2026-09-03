"""Guarded, durable ``GATED -> QUEUED`` flip for one ToolCallTask row.

Phase 3 stage 7a (docs/superpowers/2026-08-29-phase3-execution-topology-design.md,
01a0518b, user-approved; ruling 3 on the gate-payload question). Mirrors
:func:`primer.session.yields.durably_mark_session_resumable`'s own shape
exactly, at task granularity instead of session granularity: a gated
tool call has no bus/event-key wake path of its own to publish onto (a
task's own ``gate_event_key`` is a record of WHICH gate it is waiting
on, not a channel anything subscribes to) - the RESPOND endpoint calls
this helper directly, the same way the session-level respond endpoints
call :func:`~primer.session.yields.durably_wake_session`.

Unlike the session version, there is no ``ToolCallTaskState`` value
equivalent to ``"resumable"``: :class:`~primer.claim.adapters.tool_calls.
ToolCallClaimAdapter`'s own ``eligibility_sql`` already admits
``'queued'`` directly, so this flips straight to QUEUED rather than
through an intermediate resumable state.

The resume-time :class:`~primer.model.tool_approval.ToolApprovalRecord`
write (ruling 3's cross-team-consistency rider - Wave 3's audit surfaces
read exclusively from records) does NOT happen here: it happens when the
task is re-claimed and its resumed gate is actually processed, mirroring
how the session-level write happens in the RESUME coordinator
(``primer.worker.session_resume_coordinator``), not in
``durably_wake_session`` itself. That resume-time write lands with the
executor-seam split's own task-resume-coordinator analogue.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from primer.int.claim import ClaimKind
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState

if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
    from primer.int.storage import Storage


logger = logging.getLogger(__name__)


async def durably_mark_tool_call_task_resumable(
    task: ToolCallTask,
    *,
    event_key: str,
    payload: dict[str, Any] | None,
    task_storage: "Storage[ToolCallTask]",
    engine: "ClaimEngine | None",
) -> bool:
    """Guarded, durable ``GATED -> QUEUED`` flip for one ToolCallTask row.

    This is the single source of truth for the task-granular gate's
    resume transition - the direct analogue of
    :func:`primer.session.yields.durably_mark_session_resumable`.

    Steps:

    * Stamp the resume payload into ``gate_state`` under
      ``resume_event_payload`` / ``resume_event_key`` - the SAME key
      names the session-level function uses, so a future resume
      coordinator can share the exact same payload-extraction code
      the session-level one already has.
    * ``task_storage.update_unless`` the flipped row, guarded against
      ``state == DONE`` - not a TOCTOU race with cancellation (cancel
      fan-out is not wired yet - a GATED task holds no lease, so
      nothing else can claim and terminate it out from under this
      call today), but a genuine double-fire race: the same decision
      arriving twice (an operator double-click, a re-delivered bus
      notification) must not resurrect an already-completed task by
      blindly overwriting its terminal row. Revisit this guard once
      cancel fan-out lands - a cancelled-while-gated task would also
      need covering, and ``update_unless`` only takes one forbidden
      value.
    * Re-arm the claim lease via ``engine.mark_resumable`` (the park
      dropped it, same as the session case) so the claim loop picks
      the task up WITHOUT relying on any bus - a gated task has no
      bus channel to listen on in the first place.

    Returns True when the row was advanced, False when the guard
    rejected it (already resumed, or already terminal).
    """
    if task.state != ToolCallTaskState.GATED:
        # Snapshot check: mirrors the session-level function's own
        # single-event-park guard ("a single-event park only advances
        # from parked") - a second flip for an already-resumed task is
        # a no-op, not an error.
        return False

    state = dict(task.gate_state or {})
    state["resume_event_payload"] = dict(payload or {})
    state["resume_event_key"] = event_key
    updated = task.model_copy(update={
        "state": ToolCallTaskState.QUEUED,
        "gate_state": state,
    })
    landed = await task_storage.update_unless(
        updated, field="state", forbidden=ToolCallTaskState.DONE.value,
    )
    if landed is None:
        return False
    if engine is not None:
        await engine.mark_resumable(ClaimKind.TOOL_CALL, task.id)
    return True


__all__ = ["durably_mark_tool_call_task_resumable"]
