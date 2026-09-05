"""Tool-wait batch resume coordinator for the worker pool.

Phase 3 stage 7a (docs/superpowers/2026-08-29-phase3-execution-topology-design.md,
01a0518b). The sibling of :mod:`primer.worker.session_resume_coordinator`
for a session parked on a batch of independently-claimed
``ToolCallTask``s (``parked_state.kind == "tool_wait"``) instead of a
single ``Yielded`` yield. Routed here by
:meth:`primer.worker.pool.WorkerPool._select_resume_handler`, which
peeks the park kind BEFORE ``session_resume_coordinator`` is ever
entered — that module's own rehydration assumes a ``Yielded``-shaped
blob and must stay untouched by this arc (see its own tripwire).

Approved shape (leader ruling, 01a0518b): by the time this runs, every
sibling ``ToolCallTask`` for the parked turn is already terminal (the
last one's ``on_release`` is what re-armed the session's claim lease in
the first place — ruling 2). This coordinator's job is a pure
read-and-materialize step, NOT re-execution:

1. Read every sibling task by id — the ids come straight from the
   ``ToolWaitParkedState`` blob (``outstanding_task_ids`` +
   ``notifying_task_ids``), not a live ``(session_id, turn_no)`` storage
   query (see that dataclass's own docstring for why direct-id lookup
   was chosen over a predicate query for this first cut).
2. Assemble the SAME ``[assistant_tool_use_msg, tool-role Message(N
   ToolResultParts)]`` shape :func:`primer.agent.loop._dispatch_tool_calls`
   already builds for the in-process case, from each sibling's
   ``result_state`` — order-independent (ruling: the original design's
   own "order-independent result assembly" goal), since every
   ``ToolResultPart`` carries its own ``id`` and adapters pair by id,
   not position.
3. Append that delta via the same persistence seam
   ``session_resume_coordinator.inject_resume_and_continue`` uses
   (single in-process writer, existing lock — the persist-and-handoff
   finding: no new writer concurrency, the atomic-seq question does
   not apply here).
4. Publish the tick, release with ``drop_lease=False`` — the SAME
   "next claim runs an ordinary turn" handoff the session-level resume
   already uses; no special "continue the turn" call is needed.

NOT YET WIRED: gated-then-resumed siblings' ``ToolApprovalRecord``
write (ruling 3's cross-team-consistency rider) — no caller can produce
a GATED ToolCallTask yet (the claim-based tool-call WORKER that would
claim, execute, and possibly gate a QUEUED task hasn't landed), so
every sibling this function ever sees today is DONE, never GATED. Add
that write here, sibling to step 2 above, once that worker exists —
tracked as a follow-up in the same arc, not a silent gap: a GATED
sibling reaching this function today would simply have no
``result_state`` to assemble from, which the guard below turns into a
loud failure rather than a malformed tool_result.

REQUIRED INVARIANT FOR THE FUTURE CLAIM-BASED TOOL-CALL WORKER (design
note, 01a0518b review — pin now so it can't get lost before that
worker is built): ``primer.session.dispatch``'s crash-retry doctrine
(``_create_tool_call_task_idempotent``) only covers a BYTE-IDENTICAL
replay — the crashed attempt's re-run mints the same scoped ids
because the LLM re-emits the same tool calls in the same order. LLM
non-determinism means this is not guaranteed: a re-run can legitimately
emit a DIFFERENT batch, in which case the crashed attempt's own
QUEUED ``ToolCallTask`` rows (+ their claim-engine leases) never get
referenced by the NEW attempt's park at all — they become orphans, a
different failure mode from the mismatch case
``_create_tool_call_task_idempotent`` already raises loudly on (that
one fires when a NEW id collides with an old one under a different
record_seq; an orphan is the reverse — an OLD id nothing ever
references again). This coordinator's own read-by-id-from-the-blob
design makes it immune (an orphaned id simply never appears in
``outstanding_task_ids``/``notifying_task_ids`` here), but the
claim-based worker that eventually claims and executes QUEUED
``ToolCallTask`` rows is NOT immune by construction — it must not
blindly execute whatever it dequeues:

    Before executing a claimed ``ToolCallTask``, the worker MUST
    verify the task's id is still referenced by the OWNING session's
    live ``tool_wait`` ``parked_state`` (or an equivalent liveness
    check). An id that is no longer referenced is an orphan from a
    since-abandoned turn attempt — mark it terminal/orphaned (never
    QUEUED again) and never execute its tool call. Executing an
    orphan means running a tool call the session never actually
    committed to for its CURRENT turn.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.int.claim import Lease as ClaimLease
    from primer.model.tool_call_task import ToolCallTask
    from primer.model.workspace_session import WorkspaceSession
    from primer.worker.pool import WorkerPool

logger = logging.getLogger(__name__)


async def resume_engine_tool_wait(
    pool: "WorkerPool", engine_lease: "ClaimLease", session: "WorkspaceSession",
):
    """Drive a tool_wait-parked session's batch to conclusion.

    Read-and-materialize only (see module docstring): every sibling
    ``ToolCallTask`` is already terminal by construction (that's what
    re-armed this session's lease). Fails the session (ENDED/failed) on
    any structural surprise — a missing task row, a non-terminal task,
    or a persist failure — mirroring
    ``session_resume_coordinator.resume_engine_session``'s own
    fail-closed posture.
    """
    from primer.int.claim import ReleaseOutcome
    from primer.model.chat import Message, ToolResultPart
    from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
    from primer.worker.yield_runtime import ToolWaitParkedState

    sid = session.id
    blob = session.parked_state or {}
    try:
        parked = ToolWaitParkedState.from_jsonable(blob)
    except (KeyError, ValueError, TypeError):
        logger.exception(
            "resume_engine_tool_wait: malformed tool_wait parked_state for"
            " session %s - ending failed",
            sid,
        )
        return await pool._end_session(session, reason="failed")

    if pool._storage is None:
        logger.error(
            "resume_engine_tool_wait: session %s has no storage bound -"
            " ending failed",
            sid,
        )
        return await pool._end_session(session, reason="failed")

    task_storage = pool._storage.get_storage(ToolCallTask)
    all_ids = [*parked.outstanding_task_ids, *parked.notifying_task_ids]
    tasks: list[ToolCallTask] = []
    for task_id in all_ids:
        task = await task_storage.get(task_id)
        if task is None:
            logger.error(
                "resume_engine_tool_wait: session %s missing ToolCallTask"
                " %r - ending failed",
                sid, task_id,
            )
            return await pool._end_session(session, reason="failed")
        if task.state not in (ToolCallTaskState.DONE, ToolCallTaskState.FAILED):
            logger.error(
                "resume_engine_tool_wait: session %s task %r not terminal"
                " (state=%s) - ending failed",
                sid, task_id, task.state,
            )
            return await pool._end_session(session, reason="failed")
        tasks.append(task)

    result_parts: list[ToolResultPart] = []
    for task in tasks:
        if task.result_state is not None:
            result_parts.append(ToolResultPart.model_validate(task.result_state))
        else:
            # A FAILED task with no result_state (e.g. a poisoned claim
            # that never ran) still owes the LLM a tool_result for its
            # tool_use - synthesise an error one from last_error rather
            # than dropping the id, which would leave a dangling tool_use
            # with no pair.
            result_parts.append(ToolResultPart(
                id=task.id,
                output=task.last_error or "tool call failed",
                error=True,
            ))

    workspace = await pool._load_workspace_for_persist(session.workspace_id)
    executor_or_driver = await pool._build_agent_executor(session, workspace)
    executor = getattr(executor_or_driver, "_executor", executor_or_driver)

    rehydrated_assistant = [
        Message.model_validate(m) for m in parked.llm_messages
    ]
    tool_result_msg = Message(role="tool", parts=result_parts)
    try:
        await executor.inject_resume_messages(
            [*rehydrated_assistant, tool_result_msg],
        )
    except Exception:
        logger.exception(
            "resume_engine_tool_wait: persist failed for session %s -"
            " ending failed",
            sid,
        )
        return await pool._end_session(session, reason="failed")

    await _persist_resume_tool_result_records(pool, session, tasks)

    return ReleaseOutcome(success=True, drop_lease=False)


async def _persist_resume_tool_result_records(
    pool: "WorkerPool", session: "WorkspaceSession", tasks: "list[ToolCallTask]",
) -> None:
    """Write the modern TOOL_RESULT counterpart for every resumed task.

    Batch-shaped sibling of ``session_resume_coordinator.
    _persist_resume_tool_result_record`` — same reasoning (secondary,
    display-side record; the turn's continuation already stands on
    ``inject_resume_messages`` having succeeded), extended to write one
    record per sibling task instead of one for a single park.
    Best-effort: a write failure here is logged and swallowed, exactly
    as the single-task version does.
    """
    if pool._storage is None:
        return
    from primer.model.workspace_session import (
        SessionMessageKind,
        SessionMessageRecord,
        WorkspaceSession,
    )
    from primer.session.persistence import WorkspaceMessageWriter

    try:
        ws = await pool._load_workspace_for_persist(session.workspace_id)
        writer = WorkspaceMessageWriter(
            workspace_io=ws, session_id=session.id, start_seq=session.last_seq,
        )
        new_seq = session.last_seq
        for task in tasks:
            result = task.result_state or {}
            new_seq = await writer.append(SessionMessageRecord(
                seq=1,  # overwritten by the writer's monotonic counter
                kind=SessionMessageKind.TOOL_RESULT,
                payload={
                    "call_id": task.id,
                    "output": result.get("output", task.last_error),
                    "error": result.get("error", task.state == "failed"),
                },
                created_at=datetime.now(timezone.utc),
            ))
        await writer.flush()
        storage = pool._storage.get_storage(WorkspaceSession)
        await storage.update(session.model_copy(update={"last_seq": new_seq}))
        if pool._event_bus is not None:
            try:
                await pool._event_bus.publish(
                    f"session:{session.id}:tick", {"seq": new_seq},
                )
            except Exception:  # noqa: BLE001 - advisory
                logger.exception(
                    "resume_engine_tool_wait: tick publish failed for"
                    " session %s",
                    session.id,
                )
    except Exception:  # noqa: BLE001 - best-effort, see docstring
        logger.exception(
            "resume_engine_tool_wait: failed to persist modern TOOL_RESULT"
            " records for session %s",
            session.id,
        )


__all__ = ["resume_engine_tool_wait"]
