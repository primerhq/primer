"""Worker-side session-turn dispatch.

One ``run_one_session_turn`` invocation per claimed session lease.  The
worker pool calls this with the :class:`Lease` it received from the
:class:`ClaimEngine`; the function drives one full execution turn,
persists every :class:`StreamEvent` as a :class:`SessionMessageRecord`
to the workspace's ``messages.jsonl`` via :class:`WorkspaceMessageWriter`,
publishes a ``session:{sid}:tick`` event per record so live WebSocket
subscribers see real-time deltas, honours cancel signals delivered over
the event bus, and handles :class:`YieldToWorker` parks.

Return value:
  A :class:`ReleaseOutcome` the caller passes to
  ``engine.release(lease, outcome=...)``:
  - Normal completion: ``ReleaseOutcome(success=True, drop_lease=True)``
  - Parked (YieldToWorker): ``ReleaseOutcome(success=True, drop_lease=False)``
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from primer.int.claim import ClaimKind, Lease, ReleaseOutcome
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.turn_log import (
    TurnLogCancelled,
    TurnLogCompleted,
    TurnLogFailed,
    TurnLogResumed,
    TurnLogStarted,
    TurnLogYielded,
)
from primer.model.yield_ import YieldToWorker
from primer.session.persistence import (
    WorkspaceIO,
    WorkspaceMessageWriter,
    _CoalesceState,
    translate_stream_event,
)
from primer.observability.turn_log_writer import (
    NoopTurnLogWriter,
    TurnLogWriter,
    safe_append as _safe_turn_log,
    to_problem_details,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


def _default_turn_log_factory(
    workspace_io: WorkspaceIO, session_id: str,
) -> TurnLogWriter:
    return NoopTurnLogWriter()


@dataclass
class SessionDispatchDeps:
    """Bundle of runtime dependencies the worker injects per session task."""

    storage_provider: StorageProvider
    workspace_io: WorkspaceIO
    event_bus: EventBus

    # Callable that receives a WorkspaceSession row and returns an executor
    # whose ``invoke(messages)`` is an async generator of StreamEvents.
    # Type: Callable[[WorkspaceSession], Awaitable[Any]]
    build_executor: Callable[[WorkspaceSession], Awaitable[Any]]

    # Factory for the per-turn TurnLogWriter. Receives the workspace IO
    # and the session id so the production wiring can build a path-bound
    # writer pointed at .state/sessions/<sid>/turns.jsonl. Default is the
    # Noop writer so legacy callers (and existing tests that don't care
    # about turn-log emission) keep working.
    turn_log_writer_factory: Callable[
        [WorkspaceIO, str], TurnLogWriter,
    ] = _default_turn_log_factory


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_one_session_turn(
    lease: Lease,
    deps: SessionDispatchDeps,
) -> ReleaseOutcome:
    """Drive a single session turn; persist records; honour cancel/yield.

    Args:
        lease: The claim lease (``kind=ClaimKind.SESSION``).
        deps:  Runtime dependencies bundle.

    Returns:
        :class:`ReleaseOutcome` for the caller to pass to
        ``engine.release(lease, outcome=...)``.
    """
    assert lease.kind == ClaimKind.SESSION, (
        f"run_one_session_turn called with wrong kind: {lease.kind!r}"
    )
    session_id = lease.entity_id

    # ------------------------------------------------------------------
    # 1. Load session row
    # ------------------------------------------------------------------
    session_storage = deps.storage_provider.get_storage(WorkspaceSession)
    session = await session_storage.get(session_id)
    if session is None:
        logger.warning("session %s vanished before dispatch", session_id)
        return ReleaseOutcome(success=False, drop_lease=True)

    # Early-exit checks that don't need an executor:
    # * If the row is already ENDED (lease leaked through somehow) just
    #   drop the lease; nothing to do.
    # * If cancel_requested is set on the row — set by REST cancel before
    #   any worker observed it, or carried over from a previous process
    #   that died mid-turn — transition to ENDED/cancelled without
    #   running another turn. This is what makes "I cancelled it but
    #   nothing happened" actually terminate after the api restarts.
    if session.status == SessionStatus.ENDED:
        return ReleaseOutcome(success=True, drop_lease=True)
    if session.cancel_requested:
        session.status = SessionStatus.ENDED
        session.ended_reason = "cancelled"
        session.ended_at = _now()
        await session_storage.update(session)
        return ReleaseOutcome(success=True, drop_lease=True)

    # ------------------------------------------------------------------
    # 2. Build executor
    # ------------------------------------------------------------------
    executor = await deps.build_executor(session)
    if executor is None:
        logger.warning("executor builder returned None for session %s", session_id)
        return ReleaseOutcome(success=False, drop_lease=True)

    # ------------------------------------------------------------------
    # 3. Open WorkspaceMessageWriter + cancel-watcher
    # ------------------------------------------------------------------
    writer = WorkspaceMessageWriter(
        workspace_io=deps.workspace_io,
        session_id=session_id,
    )
    turn_log = deps.turn_log_writer_factory(deps.workspace_io, session_id)

    # If the row carries parked_at, this turn is resuming a previously
    # parked session. Emit a `resumed` event before `started` so the UI
    # can show the wait latency.
    if session.parked_at is not None:
        wait_ms = max(
            0,
            int((_now() - session.parked_at).total_seconds() * 1000),
        )
        await _safe_turn_log(turn_log, TurnLogResumed(
            seq=0,
            ts=_now(),
            turn_no=session.turn_no,
            wait_ms=wait_ms,
            resume_kind="event_fired",
        ))

    # `started` marks the boundary just before the executor begins streaming.
    _turn_started_at = _now()
    await _safe_turn_log(turn_log, TurnLogStarted(
        seq=0,
        ts=_turn_started_at,
        turn_no=session.turn_no,
        model=None,
        input_message_count=0,
    ))

    cancel_requested = False
    cancel_reason: str = "operator_interrupt"

    cancel_event = asyncio.Event()
    cancel_task = asyncio.create_task(
        _cancel_watcher(deps.event_bus, session_id, cancel_event),
        name=f"sess-cancel-{session_id}",
    )

    # ------------------------------------------------------------------
    # 4. Stream events from executor
    # ------------------------------------------------------------------
    coalesce_state = _CoalesceState()

    try:
        async for event in executor.invoke([]):
            # Translate StreamEvent → SessionMessageRecord(s)
            result = translate_stream_event(event, coalesce_state)
            if result is None:
                # Check cancel between events even when nothing was produced
                if cancel_event.is_set():
                    cancel_requested = True
                    break
                continue

            # Normalise to list
            records: list[SessionMessageRecord]
            if isinstance(result, list):
                records = result
            else:
                records = [result]

            for rec in records:
                seq = await writer.append(rec)
                await deps.event_bus.publish(
                    f"session:{session_id}:tick", {"seq": seq}
                )

            # Honour cancel after processing the current batch
            if cancel_event.is_set():
                cancel_requested = True
                break

    except YieldToWorker as park:
        # ------------------------------------------------------------------
        # 5a. Parked turn — write YIELDED record, flush, publish tick, park
        # ------------------------------------------------------------------
        await _safe_turn_log(turn_log, TurnLogYielded(
            seq=0,
            ts=_now(),
            turn_no=session.turn_no,
            yield_kind=_classify_yield_kind(park),
            event_key=park.yielded.event_key,
        ))
        rec = _yielded_record(park)
        seq = await writer.append(rec)
        await writer.flush()
        await deps.event_bus.publish(
            f"session:{session_id}:tick", {"seq": seq}
        )
        await turn_log.aclose()
        return ReleaseOutcome(success=True, drop_lease=False)

    except Exception as exc:
        logger.exception(
            "session %s executor raised unexpected error; releasing claim",
            session_id,
        )
        await _safe_turn_log(turn_log, TurnLogFailed(
            seq=0,
            ts=_now(),
            turn_no=session.turn_no,
            duration_ms=max(
                0,
                int((_now() - _turn_started_at).total_seconds() * 1000),
            ),
            error=to_problem_details(exc),
        ))
        error_rec = SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.ERROR,
            payload={"message": "unexpected executor error", "code": "executor_error"},
            created_at=_now(),
        )
        seq = await writer.append(error_rec)
        await writer.flush()
        await deps.event_bus.publish(
            f"session:{session_id}:tick", {"seq": seq}
        )
        await _transition_session_status(
            session_storage,
            session,
            new_status=SessionStatus.ENDED,
            ended_reason="failed",
        )
        await turn_log.aclose()
        return ReleaseOutcome(success=False, drop_lease=True)

    finally:
        cancel_task.cancel()
        try:
            await cancel_task
        except (asyncio.CancelledError, Exception):
            pass

    # ------------------------------------------------------------------
    # 5b. Cancel path — write CANCELLED record, transition row to ENDED
    # ------------------------------------------------------------------
    if cancel_requested:
        await _safe_turn_log(turn_log, TurnLogCancelled(
            seq=0,
            ts=_now(),
            turn_no=session.turn_no,
            reason=cancel_reason,
        ))
        rec = _cancelled_record(cancel_reason)
        seq = await writer.append(rec)
        await writer.flush()
        await deps.event_bus.publish(
            f"session:{session_id}:tick", {"seq": seq}
        )
        await _transition_session_status(
            session_storage,
            session,
            new_status=SessionStatus.ENDED,
            ended_reason="cancelled",
        )
        await turn_log.aclose()
        return ReleaseOutcome(success=True, drop_lease=True)

    # ------------------------------------------------------------------
    # 6. Clean completion — write DONE record (if not already written by
    #    translate_stream_event), flush, final tick, then transition the
    #    scheduler-visible row based on what the executor did.
    # ------------------------------------------------------------------
    await writer.flush()

    last_done_reason = getattr(executor, "last_done_reason", None)
    agent_status = await _read_agent_session_status(executor)
    new_status, ended_reason = _post_turn_status(last_done_reason, agent_status)
    await _transition_session_status(
        session_storage,
        session,
        new_status=new_status,
        ended_reason=ended_reason,
    )

    await _safe_turn_log(turn_log, TurnLogCompleted(
        seq=0,
        ts=_now(),
        turn_no=session.turn_no,
        duration_ms=max(
            0,
            int((_now() - _turn_started_at).total_seconds() * 1000),
        ),
        finish_reason=last_done_reason,
    ))
    await turn_log.aclose()
    return ReleaseOutcome(success=True, drop_lease=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Maps the actual event_key prefixes emitted by the toolset / tool_manager
# / graph paths to the three turn-log yield_kind enum values. Sources:
#   primer/toolset/misc.py:336        "ask_user:<sid>:<tcid>"
#   primer/agent/tool_manager.py:342  "tool_approval:<sid_or_chat>:<call.id>"
#   primer/graph/base.py:1702         approval-yield key (also tool_approval:)
#   primer/toolset/misc.py:212        "timer:<tcid>"
#   primer/toolset/workspaces.py:511  "watch:<sid>:<tcid>"
#   primer/toolset/mcp.py:223         "mcp_task:<tsid>:<task_id>"
#   primer/toolset/trigger.py:703     "trigger:<tid>"
# Order matches the most-specific prefix-first principle so "tool_approval:"
# doesn't accidentally match an earlier shorter prefix.
_YIELD_KIND_PREFIXES = (
    ("tool_approval:", "approval"),
    ("ask_user:", "ask_user"),
)


def _classify_yield_kind(park: YieldToWorker) -> str:
    """Map a YieldToWorker.event_key prefix to the turn-log yield_kind enum.

    Returns "approval" for tool-approval yields, "ask_user" for the
    ask_user tool, and "subscribe_to_trigger" for every other source
    (timers, watch, mcp_task, trigger, ...) since they all subscribe to
    an external event-bus key.
    """
    key = park.yielded.event_key or ""
    for prefix, kind in _YIELD_KIND_PREFIXES:
        if key.startswith(prefix):
            return kind
    return "subscribe_to_trigger"


def _yielded_record(park: YieldToWorker) -> SessionMessageRecord:
    """Build a YIELDED SessionMessageRecord from a YieldToWorker exception."""
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.YIELDED,
        payload={
            "event_key": park.yielded.event_key,
            "tool_name": park.yielded.tool_name,
            "tool_call_id": park.tool_call_id,
        },
        created_at=_now(),
    )


def _cancelled_record(reason: str) -> SessionMessageRecord:
    """Build a CANCELLED SessionMessageRecord."""
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.CANCELLED,
        payload={"reason": reason},
        created_at=_now(),
    )


async def _read_agent_session_status(executor) -> SessionStatus | None:
    """Read the on-disk AgentSession's status after a clean turn.

    The agent executor (see primer/agent/workspace_executor.py) sets
    the AgentSession status as a side effect: ENDED on stop_reason=error,
    WAITING when the assistant ends with a question, etc. The dispatch
    propagates that decision to the scheduler-visible WorkspaceSession
    row. Returns None if the executor doesn't expose ``.session.status()``.
    """
    inner = getattr(executor, "session", None)
    if inner is None:
        return None
    status_fn = getattr(inner, "status", None)
    if status_fn is None:
        return None
    try:
        return await status_fn()
    except Exception:  # noqa: BLE001
        return None


# Mapping from Done.stop_reason -> (new_status, ended_reason).
# Mirrors primer/worker/pool.py::_infer_post_turn_status, but here we
# prefer a terminal ENDED transition for clean stops so a one-shot
# session (the common UI flow) actually ends instead of looping on
# the same input forever. tool_use still leaves status RUNNING — the
# worker will pick up the next turn that the executor itself queues.
_STOP_REASON_TO_STATUS: dict[str, tuple[SessionStatus, str | None]] = {
    "stop": (SessionStatus.ENDED, "completed"),
    "end_turn": (SessionStatus.ENDED, "completed"),
    "stop_sequence": (SessionStatus.ENDED, "completed"),
    "tool_use": (SessionStatus.RUNNING, None),
    "max_tokens": (SessionStatus.WAITING, None),
    "error": (SessionStatus.ENDED, "failed"),
    "content_filter": (SessionStatus.WAITING, None),
    "graph_ended": (SessionStatus.ENDED, "completed"),
}


def _post_turn_status(
    last_done_reason: str | None,
    agent_status: SessionStatus | None,
) -> tuple[SessionStatus, str | None]:
    """Decide the WorkspaceSession.status to write after a clean turn.

    Precedence: a definitive AgentSession decision wins (the executor
    set ENDED on internal error, WAITING on a user-input prompt heuristic,
    etc.). Otherwise fall back to the LLM's last stop reason. The default
    when neither is informative is ENDED/completed — a one-shot session
    shouldn't perpetually sit at RUNNING.
    """
    # An executor-set ENDED is authoritative.
    if agent_status == SessionStatus.ENDED:
        # Translate ended-but-stop-reason into a finer reason when we can.
        mapped = _STOP_REASON_TO_STATUS.get(last_done_reason or "", (None, None))
        return (SessionStatus.ENDED, mapped[1] or "completed")
    # Executor-set WAITING (e.g. assistant asked a question heuristic).
    if agent_status == SessionStatus.WAITING:
        return (SessionStatus.WAITING, None)
    # Stop-reason mapping.
    if last_done_reason is None:
        return (SessionStatus.ENDED, "completed")
    mapped = _STOP_REASON_TO_STATUS.get(last_done_reason)
    if mapped is None:
        return (SessionStatus.ENDED, "completed")
    return mapped


async def _transition_session_status(
    session_storage,
    session: WorkspaceSession,
    *,
    new_status: SessionStatus,
    ended_reason: str | None = None,
) -> None:
    """Update the WorkspaceSession row in storage. Idempotent on no-op."""
    # Re-read the current row so we don't overwrite concurrent changes.
    fresh = await session_storage.get(session.id)
    if fresh is None:
        return
    if fresh.status == new_status and (
        ended_reason is None or fresh.ended_reason == ended_reason
    ):
        return
    updates: dict[str, object | None] = {"status": new_status}
    if new_status == SessionStatus.ENDED:
        updates["ended_at"] = datetime.now(timezone.utc)
        if ended_reason is not None:
            updates["ended_reason"] = ended_reason
    try:
        await session_storage.update(fresh.model_copy(update=updates))
    except Exception:  # noqa: BLE001
        logger.exception(
            "dispatch: failed to transition session %s to %s",
            session.id, new_status.value,
        )


async def _cancel_watcher(
    event_bus: EventBus,
    session_id: str,
    cancel_event: asyncio.Event,
) -> None:
    """Subscribe to the event bus and set cancel_event when cancel fires."""
    sub = event_bus.subscribe()
    try:
        async for event in sub:
            if event.event_key == f"session:{session_id}:cancel":
                cancel_event.set()
                return
    except asyncio.CancelledError:
        return
    finally:
        await sub.aclose()


__all__ = ["SessionDispatchDeps", "run_one_session_turn"]
