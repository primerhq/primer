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
  - Parked (YieldToWorker): ``ReleaseOutcome(success=True, drop_lease=True,
    park=ParkRequest(...))`` - lease dropped, park columns written by
    the session adapter's on_release.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from collections.abc import Awaitable, Callable

from primer.int.claim import ClaimKind, Lease, ParkRequest, ReleaseOutcome
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
import primer.observability.metrics as _metrics
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.workspace import Workspace
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
from primer.session.enqueue import SessionWakeDeps
from primer.session.delegation import (
    DelegationRecorder,
    reset_delegation_sink,
    set_delegation_sink,
)
from primer.session.mutation_lock import session_lifecycle_lock
from primer.session.pending_messages import realize_next_pending
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

    # Optional channel dispatcher. When set, a session that parks on an
    # ask_user / tool-approval gate forwards the prompt to every channel
    # associated with the session's workspace (Slack/Telegram/Discord).
    # None -> no channel forwarding (the park still succeeds).
    channel_dispatcher: Any | None = None

    # Optional registries for resolving ask_user/inform `files` into media
    # attached to the channel prompt. Both must be set for file attachments to
    # resolve; None -> files are ignored.
    workspace_registry: Any | None = None
    artifact_registry: Any | None = None

    # Wake wiring for the drain checkpoint: realizing a queued steer goes
    # through wake_session, which needs the scheduler and claim engine to
    # arm the next turn. Optional because unit-test pools build deps
    # without them; absent means queued steers simply wait for the next
    # checkpoint that does have the wiring.
    scheduler: Any | None = None
    claim_engine: Any | None = None


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
    # * If pause_requested is set, the operator paused the session while it
    #   was running or parked. Honour it BEFORE building the executor or
    #   resuming: transition to PAUSED and drop the lease without running a
    #   turn. parked_* columns are left untouched so a parked session keeps
    #   its 'resumable' marker and a later /resume can replay the hook. This
    #   check was lost when the worker turn loop moved out of pool.py
    #   (_run_one_turn) into this function; without it a paused parked
    #   session gets silently resumed to completion (e2e t0867).
    if session.pause_requested:
        # Serialize the status transition + interrupt-flag clear against a
        # concurrent resume/pause/cancel/interrupt API call (T0432-style
        # lost update; see primer.session.mutation_lock). A stale
        # interrupt_requested carried into the PAUSED row must not leak
        # into the turn that eventually resumes it, else it could downgrade
        # a later genuine Cancel to a Stop.
        async with session_lifecycle_lock().acquire(session_id):
            await _transition_session_status(
                session_storage,
                session,
                new_status=SessionStatus.PAUSED,
            )
            await _clear_interrupt_requested(session_storage, session_id)
        # Drop the lease but preserve the park columns: a paused session that
        # was 'resumable' keeps its marker + parked_state so a later /resume
        # re-arms the lease and replays the hook. preserve_park also blocks
        # the turn_no bump (no turn ran).
        return ReleaseOutcome(
            success=True, drop_lease=True, preserve_park=True,
        )

    # ------------------------------------------------------------------
    # 1.5 Consume the claimable signal before running the turn.
    # ------------------------------------------------------------------
    # wake_session() sets turn_status="claimable" on every steer, INCLUDING
    # the case where a worker already holds this session's lease (steering
    # a RUNNING session). That write can't create a new claim by itself
    # (the lease is already held -- ClaimEngine.upsert on an already-claimed
    # lease is a no-op on claimed_by), so the only way the queued
    # instruction is not stranded is for THIS turn to notice, once it's
    # done, that a steer landed. Consuming the signal here (flipping it
    # back to "idle" under the session lifecycle lock, same lock
    # wake_session uses for its own read-modify-write) means:
    #   * a wake_session() call that lands BEFORE this point is exactly
    #     what executor.invoke() below will read from messages.jsonl (a
    #     one-shot snapshot taken at invoke() entry) -- no re-arm needed.
    #   * a wake_session() call that lands AFTER this point (during the
    #     turn, or in the gap before release) re-sets turn_status to
    #     "claimable" again, which the caller (WorkerPool._run_engine_session
    #     / _maybe_rearm_session) detects post-release and re-arms a fresh
    #     lease for -- since every ReleaseOutcome this function returns
    #     drops the lease.
    # Without this consume step a turn_status="claimable" written by a much
    # earlier, already-serviced steer would never be cleared (nothing else
    # writes turn_status back to "idle") and would spin-claim this session
    # forever.
    # Capture the row's CURRENT last_seq under the lifecycle lock — the SAME
    # lock wake_session/reset_session hold for their own USER_INPUT / divider
    # seq writes — so the per-turn writer is seeded with a value that already
    # reflects any seq a preceding wake_session wrote before it pulsed the
    # scheduler. This is the authoritative fresh read (not the possibly-cached
    # `session` object loaded above), so it also covers the case where the
    # worker's `session` snapshot predates that write.
    seed_seq = session.last_seq
    async with session_lifecycle_lock().acquire(session_id):
        fresh_before_turn = await session_storage.get(session_id)
        if fresh_before_turn is not None:
            seed_seq = fresh_before_turn.last_seq
            if fresh_before_turn.turn_status == "claimable":
                await session_storage.update(
                    fresh_before_turn.model_copy(update={"turn_status": "idle"})
                )

    # ------------------------------------------------------------------
    # 2. Build executor
    # ------------------------------------------------------------------
    # Building the executor can raise a fatal resolution error BEFORE the
    # turn starts streaming -- e.g. a graph-bound session whose graph row
    # was deleted (NotFoundError at resolve), a missing agent, or a
    # ConfigError. This call sits OUTSIDE the streaming try/except below,
    # so an escaping exception would otherwise propagate uncaught up to the
    # worker's _run_engine_session, which only logs it -- leaving the
    # session stuck RUNNING forever (e2e t0624). Converge to ENDED/failed
    # here so the row always reaches a terminal state and the lease drops.
    try:
        executor = await deps.build_executor(session)
    except Exception:
        logger.exception(
            "session %s failed to build executor; ending failed",
            session_id,
        )
        # Serialize the terminal transition + interrupt-flag clear against
        # a concurrent resume/pause/cancel/interrupt API call (T0432-style
        # lost update; see primer.session.mutation_lock).
        async with session_lifecycle_lock().acquire(session_id):
            await _transition_session_status(
                session_storage,
                session,
                new_status=SessionStatus.ENDED,
                ended_reason="failed",
            )
            await _clear_interrupt_requested(session_storage, session_id)
        return ReleaseOutcome(success=False, drop_lease=True)
    if executor is None:
        logger.warning("executor builder returned None for session %s", session_id)
        return ReleaseOutcome(success=False, drop_lease=True)

    # ------------------------------------------------------------------
    # 3. Open WorkspaceMessageWriter + cancel-watcher
    # ------------------------------------------------------------------
    writer = WorkspaceMessageWriter(
        workspace_io=deps.workspace_io,
        session_id=session_id,
        # Seed past the row's existing history so this turn's records continue
        # the per-session (session_id, seq) sequence monotonically instead of
        # restarting at seq=1 and colliding with prior turns / USER_INPUT rows.
        start_seq=seed_seq,
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
    await _event_recorder(deps).emit(
        "turn.started",
        workspace_id=session.workspace_id,
        session_id=session_id,
        payload={"turn_no": session.turn_no},
    )
    await _safe_turn_log(turn_log, TurnLogStarted(
        seq=0,
        ts=_turn_started_at,
        turn_no=session.turn_no,
        model=None,
        input_message_count=0,
    ))

    # Intentionally no start acknowledgement here. Per-session channel threads
    # are created LAZILY: the first eager post to a workspace's reply binding
    # is what GET-OR-CREATES the Discord/Slack per-session thread, so posting a
    # "started" ack on turn 0 of EVERY session that happens to run in a
    # binding-bearing workspace (background/graph/test sessions included) opened
    # an empty thread the session never used. There is no per-session
    # channel-origin marker to gate on -- channel-triggered sessions reach the
    # channel through the same workspace-standing Workspace.reply_binding every
    # other session uses -- so the start ack is dropped entirely. A thread now
    # forms only on the first REAL outbound signal: a gate forward / inform
    # (post_prompt) or a non-empty final result.

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

    # Subagent runs execute inline in this turn with no writer of
    # their own, so the recorder is published here and picked up by
    # the invoke loops through a contextvar. Without it a delegated
    # run leaves only an opaque tool call in the transcript.
    _delegation_token = set_delegation_sink(DelegationRecorder(
        writer=writer, event_bus=deps.event_bus, session_id=session_id,
    ))

    # Sessions currently executing a turn, by workspace. Six writers mutate
    # SessionStatus outside the lifecycle lock, so a transition-delta gauge
    # would drift; this try/finally is the one exact chokepoint (park,
    # error, cancel and clean exits all run the finally below).
    _metrics.sessions_active.labels(session.workspace_id).inc()

    # Ephemeral delta stream: the live content the durable log omits, on a
    # separate bus channel. A degraded bus is swallowed inside the buffer, so
    # the durable record still completes each part (UI falls back gracefully).
    from primer.tap.delta import DeltaBuffer
    delta_buffer = (
        DeltaBuffer(session_id=session_id, publish=deps.event_bus.publish)
        if deps.event_bus is not None else None
    )
    if delta_buffer is not None:
        await delta_buffer.start()

    try:
        async for event in executor.invoke([]):
            # Translate StreamEvent → SessionMessageRecord(s)
            result = translate_stream_event(
                event, coalesce_state, delta_sink=delta_buffer
            )
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
                if rec.kind == SessionMessageKind.GRAPH_TRANSITION:
                    await _emit_graph_transition(deps, session, rec)

            # Honour cancel after processing the current batch
            if cancel_event.is_set():
                cancel_requested = True
                break

    except YieldToWorker as park:
        # ------------------------------------------------------------------
        # 5a. Parked turn - write YIELDED record, flush, publish tick, then
        # return a park outcome. The engine drops the lease (drop_lease=True)
        # and the session adapter's on_release writes the park columns
        # (parked_status='parked'). No lease while parked => no re-claim loop.
        # ------------------------------------------------------------------
        # Function-local import: a module-level import of yield_runtime here
        # creates a circular import (primer.worker.__init__ -> pool -> this
        # module) that only resolves because pool happens to load first.
        # Importing inside the park branch (which runs rarely) avoids that
        # fragility entirely.
        from primer.worker.yield_runtime import ParkedState

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
        await _event_recorder(deps).emit(
            "session.parked",
            workspace_id=session.workspace_id,
            session_id=session_id,
            payload={"event_key": park.yielded.event_key},
        )
        await turn_log.aclose()

        yielded = park.yielded
        parked_at = _now()
        # Per-yield timeout takes precedence; fall back to the global yield
        # cap (60 min default).
        timeout = yielded.timeout if yielded.timeout is not None else 3600.0
        parked_until = parked_at + timedelta(seconds=timeout)

        # Stamp parked_at_iso into resume_metadata so the resume hook can
        # compute elapsed without a separate read.
        resume_metadata = dict(yielded.resume_metadata)
        resume_metadata["parked_at_iso"] = parked_at.isoformat()
        yielded_stamped = type(yielded)(
            tool_name=yielded.tool_name,
            event_key=yielded.event_key,
            timeout=yielded.timeout,
            resume_metadata=resume_metadata,
            event_keys=getattr(yielded, "event_keys", None),
        )

        # Forward the prompt to every channel associated with this
        # session's workspace (ask_user / tool_approval gates). Awaited
        # so delivery is attempted before the lease drops;
        # _dispatch_to_channels never raises and no-ops when no dispatcher
        # is wired. Function-local import mirrors the ParkedState import
        # below to avoid the worker->dispatch circular import.
        from primer.worker.yield_runtime import (
            _dispatch_to_channels,
            _dispatch_to_channels_multi,
            merge_pending_dispatch,
        )

        graph_checkpoint = getattr(park, "graph_checkpoint", None)
        multi_keys = getattr(yielded, "event_keys", None)
        # Resolve workspace attribution fields for the channel prompt header.
        ws_name, sess_label = await _resolve_attribution(
            deps.storage_provider, session,
        )
        if multi_keys and graph_checkpoint:
            # Multi-event graph park: one prompt per pending node. The
            # re-park path (after a reply) never re-dispatches, so each
            # node is prompted exactly once.
            await _dispatch_to_channels_multi(
                dispatcher=deps.channel_dispatcher,
                workspace_id=session.workspace_id,
                session_id=session.id,
                pending=merge_pending_dispatch(graph_checkpoint),
                already_sent=set(),
                workspace_name=ws_name,
                session_label=sess_label,
                session=session,
            )
        else:
            await _dispatch_to_channels(
                dispatcher=deps.channel_dispatcher,
                session=session,
                yielded=yielded_stamped,
                workspace_registry=deps.workspace_registry,
                artifact_registry=deps.artifact_registry,
                workspace_name=ws_name,
                session_label=sess_label,
            )

        # The executor stamps YieldToWorker.llm_messages with the in-progress
        # turn history (the assistant message that emitted the tool_use).
        # Round-trip through model_dump so the JSONB column carries canonical
        # Primer message-dicts; ParkedState.from_jsonable rebuilds typed
        # Messages on resume.
        captured_messages = park.llm_messages or []
        llm_message_dicts = [m.model_dump(mode="json") for m in captured_messages]

        # Graph-bound ToolCalls stamp the mid-flight executor snapshot on
        # YieldToWorker.graph_checkpoint at park time; carry it through so the
        # resume dispatch can route to the graph resume adapter.
        graph_checkpoint = getattr(park, "graph_checkpoint", None)

        # A yield raised inside a NESTED invoke_agent invocation arrives with
        # ``park.frames`` already populated (run_subagent/resume_subagent
        # prepended one AgentFrame per in-flight caller). Persist that stack so
        # the worker's continuation walk can unwind it on resume. A session that
        # yielded directly carries an empty list -> the existing per-tool_name
        # resume path handles it unchanged.
        parked_state = ParkedState(
            yielded=yielded_stamped,
            llm_messages=llm_message_dicts,
            turn_no=session.turn_no,
            # Captured at park, not at resume: a switch applied while the
            # session waits bumps the row's epoch, and the resume must be
            # able to notice it is running for a binding that has been
            # replaced.
            binding_epoch=session.binding_epoch,
            # started_at is the true turn start (for resume latency reporting),
            # not the park moment; _turn_started_at was captured before the
            # executor began streaming.
            started_at=_turn_started_at,
            tool_call_id=park.tool_call_id,
            graph_checkpoint=graph_checkpoint,
            frames=list(getattr(park, "frames", []) or []),
            # Frozen at park so a fenced resume rebuilds the SAME toolset
            # even after the attachment TTL has expired: a resumed prompt
            # that disagreed with the parked one would be a silent
            # mid-turn capability change.
            client_tools_attached=_has_client_toolset(executor),
        )

        logger.info(
            "session %s parking on tool %r (event_key=%r, timeout=%.1fs)",
            session_id, yielded.tool_name, yielded.event_key, timeout,
        )

        # A park doesn't touch session.status, but a stale interrupt_requested
        # (e.g. an interrupt fired but the executor parked on a tool before
        # the cancel_event check ran, so the disambiguation branch below
        # never got a chance to consume it) must not leak into the turn
        # that eventually resumes this park.
        async with session_lifecycle_lock().acquire(session_id):
            await _clear_interrupt_requested(session_storage, session_id)
            await _persist_last_seq(session_storage, session_id, writer.last_seq)

        _observe_turn(session, "parked", _turn_started_at)
        return ReleaseOutcome(
            success=True,
            drop_lease=True,
            park=ParkRequest(
                parked_state=parked_state.to_jsonable(),
                parked_event_key=yielded.event_key,
                parked_event_keys=getattr(yielded, "event_keys", None),
                parked_until=parked_until,
                parked_at=parked_at,
            ),
        )

    except Exception as exc:
        logger.exception(
            "session %s executor raised unexpected error; releasing claim",
            session_id,
        )
        # Build the ProblemDetails envelope once and reuse it for BOTH
        # the structured turn-log event and the messages.jsonl ERROR
        # record. Operators looking at the Messages tab now see the
        # real exception type/title/detail (matching what the Turn log
        # tab shows) instead of the legacy "unexpected executor error"
        # generic string. Spec §6.1 called for the legacy string to go
        # away once the turn-log existed; this is that cutover.
        problem = to_problem_details(exc)
        await _safe_turn_log(turn_log, TurnLogFailed(
            seq=0,
            ts=_now(),
            turn_no=session.turn_no,
            duration_ms=max(
                0,
                int((_now() - _turn_started_at).total_seconds() * 1000),
            ),
            error=problem,
        ))
        error_rec = SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.ERROR,
            payload={
                # Keep `message` + `code` for backwards-compat with any
                # operator tooling that consumed the legacy shape; the
                # values now reflect the real exception instead of the
                # generic fallback.
                "message": problem.detail,
                "code": problem.type,
                "title": problem.title,
                "status": problem.status,
                "extensions": problem.extensions or {},
            },
            created_at=_now(),
        )
        # Wrap the workspace IO write so that a secondary storage failure
        # (e.g. disk full, broken workspace mount) cannot prevent the
        # session from transitioning to ENDED.  If the write fails the
        # error is logged but execution falls through to the transition
        # below, which is what guarantees the lease is always released.
        try:
            seq = await writer.append(error_rec)
            await writer.flush()
            await deps.event_bus.publish(
                f"session:{session_id}:tick", {"seq": seq}
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "session %s failed to write error record after executor"
                " failure; session will still be transitioned to ENDED",
                session_id,
            )
        async with session_lifecycle_lock().acquire(session_id):
            await _transition_session_status(
                session_storage,
                session,
                new_status=SessionStatus.ENDED,
                ended_reason="failed",
                expected_epoch=session.binding_epoch,
            )
            await _clear_interrupt_requested(session_storage, session_id)
            await _persist_last_seq(session_storage, session_id, writer.last_seq)
            await _advance_drain_cursor(session_storage, session_id)
        await _publish_terminal(
            deps, session, SessionStatus.ENDED, "failed",
        )
        await turn_log.aclose()
        await _apply_pending_switch_at_checkpoint(deps, session)
        await _realize_pending_at_checkpoint(deps, session)
        _observe_turn(session, "failed", _turn_started_at)
        return ReleaseOutcome(success=False, drop_lease=True)

    finally:
        _metrics.sessions_active.labels(session.workspace_id).dec()
        reset_delegation_sink(_delegation_token)
        cancel_task.cancel()
        try:
            await cancel_task
        except (asyncio.CancelledError, Exception):
            pass
        if delta_buffer is not None:
            await delta_buffer.aclose()

    # ------------------------------------------------------------------
    # 5b. Cancel path — write CANCELLED record, transition row to ENDED
    # ------------------------------------------------------------------
    if cancel_requested:
        # Re-read to see whether this preemption was a Stop (interrupt,
        # stay alive) or an End/Cancel (terminal). The interrupt path and
        # the cancel path both fire session:{sid}:cancel on the bus, so
        # both flags can be set on the same row (e.g. a stuck
        # interrupt_requested left over from an earlier turn that never
        # consumed it, plus a brand-new hard cancel). Cancel is always the
        # stronger intent: require interrupt_requested AND NOT
        # cancel_requested, so a genuine Cancel is never downgraded to a
        # Stop.
        fresh = await session_storage.get(session_id)
        is_interrupt = bool(
            fresh and fresh.interrupt_requested and not fresh.cancel_requested
        )
        await _safe_turn_log(turn_log, TurnLogCancelled(
            seq=0,
            ts=_now(),
            turn_no=session.turn_no,
            reason="operator_interrupt" if is_interrupt else cancel_reason,
        ))
        rec = _cancelled_record(
            "operator_interrupt" if is_interrupt else cancel_reason
        )
        seq = await writer.append(rec)
        await writer.flush()
        await deps.event_bus.publish(
            f"session:{session_id}:tick", {"seq": seq}
        )
        if is_interrupt:
            new_status, ended_reason = _interrupt_post_status()
        else:
            new_status, ended_reason = SessionStatus.ENDED, "cancelled"
        # Serialize the terminal transition + interrupt-flag clear against
        # a concurrent resume/pause/cancel/interrupt API call (T0432-style
        # lost update; see primer.session.mutation_lock). The flag is
        # cleared on BOTH branches (not just the interrupt one) so a flag
        # that lost the disambiguation above (Cancel won) can't persist
        # into a future turn either.
        async with session_lifecycle_lock().acquire(session_id):
            await _transition_session_status(
                session_storage,
                session,
                new_status=new_status,
                ended_reason=ended_reason,
                executor=executor,
                expected_epoch=session.binding_epoch,
            )
            await _clear_interrupt_requested(session_storage, session_id)
            await _persist_last_seq(session_storage, session_id, writer.last_seq)
            await _advance_drain_cursor(session_storage, session_id)
        await _publish_terminal(
            deps, session, new_status, ended_reason,
        )
        await turn_log.aclose()
        await _apply_pending_switch_at_checkpoint(deps, session)
        await _realize_pending_at_checkpoint(deps, session)
        _observe_turn(session, "cancelled", _turn_started_at)
        return ReleaseOutcome(success=True, drop_lease=True)

    # ------------------------------------------------------------------
    # 6. Clean completion — write DONE record (if not already written by
    #    translate_stream_event), flush, final tick, then transition the
    #    scheduler-visible row based on what the executor did.
    # ------------------------------------------------------------------
    await writer.flush()

    last_done_reason = getattr(executor, "last_done_reason", None)
    agent_status = await _read_agent_session_status(executor)
    new_status, ended_reason = _post_turn_status(
        last_done_reason, agent_status,
    )
    # _post_turn_status returns ended_reason "completed", "failed" or None
    # (dispatch.py:833-842: max_tokens / content_filter park the session in
    # WAITING with no ended_reason). Only "failed" is a failure; a None
    # reason means the turn ran to a clean stop the session can continue
    # from, so it counts as completed.
    _observe_turn(
        session,
        "failed" if ended_reason == "failed" else "completed",
        _turn_started_at,
    )
    # Serialize the terminal transition + interrupt-flag clear against a
    # concurrent resume/pause/cancel/interrupt API call (T0432-style lost
    # update; see primer.session.mutation_lock). A clean completion can
    # still carry a stale interrupt_requested (e.g. the interrupt fired
    # too late to be observed by this turn's cancel_event check), so clear
    # it here too -- every terminal path must, or it leaks into a future
    # turn and can downgrade a later genuine Cancel to a Stop.
    async with session_lifecycle_lock().acquire(session_id):
        await _transition_session_status(
            session_storage,
            session,
            new_status=new_status,
            ended_reason=ended_reason,
            executor=executor,
            expected_epoch=session.binding_epoch,
        )
        await _clear_interrupt_requested(session_storage, session_id)
        await _persist_last_seq(session_storage, session_id, writer.last_seq)
        await _advance_drain_cursor(session_storage, session_id)

    await _event_recorder(deps).emit(
        "session.replied",
        workspace_id=session.workspace_id,
        session_id=session_id,
        payload={
            "turn_no": session.turn_no,
            "finish_reason": last_done_reason,
        },
    )
    await _publish_terminal(
        deps, session, new_status, ended_reason,
    )

    # Every terminal exit drains, not just this one: a queued steer is
    # the user's message, and dropping it because their turn errored
    # or they hit Stop loses work silently. Realization deletes the
    # row and the queue is finite, so a failing session retries each
    # queued message at most once rather than looping.
    await _apply_pending_switch_at_checkpoint(deps, session)
    await _realize_pending_at_checkpoint(deps, session)

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

    # Final-result relay: on a clean finish, post the last-turn assistant
    # text to the session's reply binding. A thread-mapped interactive
    # session (crosscheck M4) relays after EVERY drained turn, not only at
    # session end, because a channel conversation continues turn by turn.
    relay_every_turn = bool(
        (session.metadata or {}).get(RELAY_EVERY_TURN_KEY)
    )
    if deps.channel_dispatcher is not None and (
        relay_every_turn
        or (
            new_status == SessionStatus.ENDED
            and ended_reason == "completed"
        )
    ):
        try:
            from primer.channel.session_relay import (
                post_session_final_result,
                read_session_final_text,
            )

            final_text = await read_session_final_text(
                deps.workspace_io, session_id,
            )
            if final_text:
                await post_session_final_result(
                    dispatcher=deps.channel_dispatcher,
                    session=session,
                    storage_provider=deps.storage_provider,
                    text=final_text,
                )
        except Exception:  # never block release on a relay failure
            logger.warning(
                "session %s: final-result relay failed", session_id,
                exc_info=True,
            )

    return ReleaseOutcome(success=True, drop_lease=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _binding_ref(session: WorkspaceSession) -> str:
    """Bounded turn label: the agent or graph the session is bound to.

    Bounded by the number of agent/graph definitions, never by session
    volume (12-s7-design.md section 2 decision 3).
    """
    binding = session.binding
    return (
        getattr(binding, "agent_id", None)
        or getattr(binding, "graph_id", None)
        or "unknown"
    )


def _observe_turn(
    session: WorkspaceSession, status: str, started_at: datetime,
) -> None:
    """Record one turn against the S7 turn instruments.

    Boundary is run_one_session_turn: called once on each of the four
    exits a started turn can take (parked, failed, cancelled, completed).
    """
    ref = _binding_ref(session)
    _metrics.turns_total.labels(ref, status).inc()
    _metrics.turn_duration_seconds.labels(ref, status).observe(
        max(0.0, (_now() - started_at).total_seconds())
    )


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


def _interrupt_post_status() -> tuple[SessionStatus, str | None]:
    """Stop (interrupt) leaves the session alive/idle, awaiting input."""
    return (SessionStatus.WAITING, None)


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
    "graph_failed": (SessionStatus.ENDED, "failed"),
}


def _post_turn_status(
    last_done_reason: str | None,
    agent_status: SessionStatus | None,
) -> tuple[SessionStatus, str | None]:
    """Decide the WorkspaceSession.status to write after a clean turn.

    Precedence: a definitive AgentSession decision wins (the executor
    set ENDED on internal error, WAITING on a user-input prompt heuristic,
    etc.). Otherwise fall back to the LLM's last stop reason. The default
    when neither is informative is ENDED/completed.

    Every clean agent turn ENDS the session — there is no interactive
    "stays WAITING" downgrade. Sending a NEW message to an ENDED session
    reopens it (``wake_session``'s ENDED branch), so a one-shot caller
    (trigger/webhook/API/e2e) never hangs on a session that finished
    cleanly. The executor-set WAITING (assistant-asked-a-question
    heuristic) is a distinct, legitimate wait and is preserved below.
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


def _has_client_toolset(executor: Any) -> bool:
    """Did this turn's tool manager carry the client toolset (S3 s4)?"""
    from primer.toolset.client import CLIENT_TOOLSET_ID

    inner = getattr(executor, "_executor", executor)
    manager = getattr(inner, "_tool_manager", None)
    providers = getattr(manager, "toolset_providers", None)
    return bool(providers) and CLIENT_TOOLSET_ID in providers


async def _persist_last_seq(
    session_storage, session_id: str, seq: int,
) -> None:
    """Persist the turn writer's advancing seq back to the session row.

    Seeds the NEXT turn's :class:`WorkspaceMessageWriter` (and any later
    ``wake_session`` / ``reset_session``) so ``(session_id, seq)`` stays
    strictly monotonic across turns instead of every turn restarting at
    seq=1. Re-reads the fresh row and only ADVANCES ``last_seq`` (never
    downgrades) so a concurrent steer that already wrote a higher USER_INPUT
    seq is not clobbered. Always called from inside a
    ``session_lifecycle_lock`` critical section — the same lock
    ``wake_session``/``reset_session`` use for their own ``last_seq`` writes —
    so this read-modify-write cannot interleave with a racing seq write.
    """
    fresh = await session_storage.get(session_id)
    if fresh is not None and seq > fresh.last_seq:
        await session_storage.update(
            fresh.model_copy(update={"last_seq": seq})
        )


async def _advance_drain_cursor(session_storage, session_id: str) -> None:
    """Advance the drain checkpoint cursor at a fully drained turn.

    The cursor marks where the next turn scan starts. It moves ONLY
    here, at a checkpoint the loop reached by finishing a turn, and only
    forwards: on the chat surface, advancing it mid-turn let a crash
    replay records the previous turn had already consumed.

    ``last_seq`` is by definition the highest seq assigned to this
    session, so the next unconsumed record is the one after it. That is
    why this needs no log read (plan errata E5): the loop already knows
    the turn terminated, and the row already carries the high-water
    mark. Re-reads fresh and never downgrades, so a concurrent steer
    that pushed the cursor further is not clobbered.
    """
    fresh = await session_storage.get(session_id)
    if fresh is None:
        return
    target = fresh.last_seq + 1
    if target > fresh.next_unprocessed_seq:
        await session_storage.update(
            fresh.model_copy(update={"next_unprocessed_seq": target})
        )


def _event_recorder(deps: SessionDispatchDeps):
    """Recorder over the deps refs; built per call, it is stateless."""
    from primer.events.recorder import recorder_for

    return recorder_for(deps.storage_provider, deps.event_bus)


async def _emit_graph_transition(
    deps: SessionDispatchDeps,
    session: "WorkspaceSession",
    rec: "SessionMessageRecord",
) -> None:
    """Land graph.node_entered/exited from a GRAPH_TRANSITION record.

    One site for every executor: the record loop is where node
    lifecycle already surfaces, so no recorder threading into the
    graph package is needed.
    """
    payload = rec.payload or {}
    phase = payload.get("phase")
    if phase not in ("enter", "exit"):
        return
    event_payload = {
        "graph_node_id": payload.get("node_id"),
        "node_kind": payload.get("node_kind"),
    }
    if phase == "enter":
        await _event_recorder(deps).emit(
            "graph.node_entered",
            workspace_id=session.workspace_id,
            session_id=session.id,
            payload=event_payload,
        )
    else:
        event_payload["status"] = payload.get("status")
        await _event_recorder(deps).emit(
            "graph.node_exited",
            workspace_id=session.workspace_id,
            session_id=session.id,
            payload=event_payload,
        )


async def _publish_terminal(
    deps: SessionDispatchDeps,
    session: "WorkspaceSession",
    status: SessionStatus,
    ended_reason: str | None,
) -> None:
    """Announce that this turn reached a terminal state.

    The interactive webhook hold (primer/trigger/hold.py) awaits this key
    instead of polling the row, per S6 section 9. Advisory: a publish
    failure must never block the lease release, so it is swallowed with a
    log and the hold falls back to its wait cap.

    An ENDED status additionally lands a durable ``session.ended`` on
    the platform event log (the recorder swallows its own failures).
    """
    session_id = session.id
    if status == SessionStatus.ENDED:
        await _event_recorder(deps).emit(
            "session.ended",
            workspace_id=session.workspace_id,
            session_id=session_id,
            payload={"ended_reason": ended_reason},
        )
    if deps.event_bus is None:
        return
    try:
        await deps.event_bus.publish(
            f"session:{session_id}:terminal",
            {"status": status.value, "ended_reason": ended_reason},
        )
    except Exception:  # noqa: BLE001 - advisory; never block the release
        logger.warning(
            "session %s: terminal event publish failed", session_id,
            exc_info=True,
        )


def _snapshot_resolver(deps: "SessionDispatchDeps"):
    """Resolve the live definition of a switch's incoming target.

    Returns None when the row is gone rather than raising, so a switch
    to a since-deleted agent degrades to a snapshot-less binding the
    executor builder resolves live instead of wedging the session.
    """

    async def _resolve(binding):
        from primer.model.agent import Agent
        from primer.model.graph import Graph

        try:
            if getattr(binding, "kind", None) == "graph":
                return await deps.storage_provider.get_storage(Graph).get(
                    binding.graph_id
                )
            return await deps.storage_provider.get_storage(Agent).get(
                binding.agent_id
            )
        except Exception:  # noqa: BLE001 - a missing target is not fatal
            return None

    return _resolve


async def _apply_pending_switch_at_checkpoint(
    deps: "SessionDispatchDeps", session,
) -> None:
    """Apply a switch queued during this turn, before the queue drains.

    Ordering is the point: realizing a queued steer first would run the
    user's follow-up under the OUTGOING binding, which is exactly what
    next-turn switch semantics forbid.

    Runs outside the lifecycle lock for the same reason the realize
    does, and swallows failures for the same reason too: the turn has
    already terminated and released its lease. The request stays queued
    and applies at the next checkpoint.
    """
    try:
        sessions = deps.storage_provider.get_storage(WorkspaceSession)
        fresh = await sessions.get(session.id)
        if fresh is None or fresh.pending_binding_switch is None:
            return
        from primer.session.binding_switch import apply_binding_switch

        await apply_binding_switch(
            sessions=sessions,
            workspace_io=deps.workspace_io,
            row=fresh,
            request=fresh.pending_binding_switch,
            actor=str(fresh.pending_binding_switch.get("actor") or "system"),
            resolve_snapshot=_snapshot_resolver(deps),
        )
    except Exception:
        logger.exception(
            "drain checkpoint: applying a queued binding switch failed for %s",
            session.id,
        )


async def _realize_pending_at_checkpoint(
    deps: "SessionDispatchDeps", session, 
) -> None:
    """Turn exactly one queued steer into a real turn.

    A steer that arrived while this turn was open was stored as a
    seq-less pending row rather than written into the log. The turn has
    now terminated, so the queue head can safely become a USER_INPUT and
    arm the next turn.

    Exactly one, because realizing the whole queue would write several
    user messages against a single turn and break the 1:1 pairing the
    drain counts. The rest follow at later checkpoints.

    Failures are swallowed: the turn already reached a terminal state and
    released its lease, so a storage hiccup here must not unwind that.
    The row stays queued for the next checkpoint.
    """
    if deps.scheduler is None or deps.claim_engine is None:
        return
    if deps.workspace_registry is None:
        return
    try:
        wake_deps = SessionWakeDeps(
            storage_provider=deps.storage_provider,
            scheduler=deps.scheduler,
            claim_engine=deps.claim_engine,
            workspace_registry=deps.workspace_registry,
            event_bus=deps.event_bus,
        )
        await realize_next_pending(
            storage_provider=deps.storage_provider,
            workspace_id=session.workspace_id,
            session_id=session.id,
            wake_deps=wake_deps,
        )
    except Exception:
        logger.exception(
            "drain checkpoint: realizing a queued steer failed for %s",
            session.id,
        )


async def _clear_interrupt_requested(session_storage, session_id: str) -> None:
    """Best-effort clear of a (possibly stale) ``interrupt_requested`` flag.

    Called on every terminal/park exit from :func:`run_one_session_turn`
    (clean completion, build/executor failure, park, and both branches of
    the cancel/interrupt disambiguation) so a flag this turn didn't
    consume -- e.g. the turn parked or failed before the cancel_event
    check ever ran, or a concurrent Cancel won the disambiguation instead
    -- cannot leak into a future turn and downgrade a later genuine
    Cancel to a Stop. Always called from inside a
    ``session_lifecycle_lock`` critical section alongside the terminal
    transition so the two writes can't interleave with a racing
    resume/pause/cancel/interrupt API call.
    """
    fresh = await session_storage.get(session_id)
    if fresh is not None and fresh.interrupt_requested:
        await session_storage.update(
            fresh.model_copy(update={"interrupt_requested": False})
        )


async def _transition_session_status(
    session_storage,
    session: WorkspaceSession,
    *,
    new_status: SessionStatus,
    ended_reason: str | None = None,
    executor=None,
    expected_epoch: int | None = None,
) -> None:
    """Update the WorkspaceSession row in storage. Idempotent on no-op.

    When ``new_status`` is ENDED and an ``executor`` is supplied, the
    terminal status is ALSO mirrored onto the executor's on-disk
    :class:`AgentSession` slot (``session.json``). The scheduler-visible
    row (postgres) and the workspace-visible slot (on disk) are two
    separate views of the same session: the worker decides ENDED here and
    writes the row, but the executor's AgentSession was left at RUNNING
    after a clean ``stop`` turn (it only self-ends on internal error /
    WAITING). Without this mirror the workspace tools that read the slot
    -- ``workspaces__get_workspace_session`` /
    ``list_workspace_sessions`` (and the cross-process rehydration in
    ``LocalWorkspace.get_session``) -- report a terminated session as
    permanently ``running``, because the worker ran in a different process
    (or workspace-cache instance) than the one those reads resolve.
    """
    # Re-read the current row so we don't overwrite concurrent changes.
    fresh = await session_storage.get(session.id)
    if fresh is None:
        return
    if expected_epoch is not None and fresh.binding_epoch != expected_epoch:
        # The binding switched while this turn ran. The terminal status
        # describes work done for a binding the session has left, so
        # writing it would clobber the switch that replaced it. The next
        # turn writes its own status under the current binding.
        logger.info(
            "session %s: voiding a terminal write from epoch %s "
            "(row is at epoch %s)",
            session.id, expected_epoch, fresh.binding_epoch,
        )
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
    if new_status == SessionStatus.ENDED:
        await _sync_agent_session_ended(executor, ended_reason)


async def _sync_agent_session_ended(executor, ended_reason: str | None) -> None:
    """Mirror a terminal ENDED transition onto the on-disk AgentSession slot.

    Commits ``session.json`` (status=ENDED) so the workspace-side reads
    (``get_session`` / ``list_sessions``) agree with the scheduler row.
    Best-effort: a missing executor / already-ENDED slot / commit failure
    must never block the lease release, so every branch is swallowed with a
    log. ``ended_reason`` is constrained to the three terminal reasons the
    AgentSession transition table accepts; an unknown value falls back to
    ``"completed"`` so the on-disk slot still reaches a terminal state.
    """
    inner = getattr(executor, "session", None) if executor is not None else None
    set_status = getattr(inner, "set_status", None)
    if set_status is None:
        return
    try:
        current = await inner.status()
        if current == SessionStatus.ENDED:
            return
        reason = ended_reason if ended_reason in (
            "completed", "failed", "cancelled",
        ) else "completed"
        await set_status(SessionStatus.ENDED, ended_reason=reason)
    except Exception:  # noqa: BLE001 -- advisory; never block release
        logger.warning(
            "dispatch: failed to mirror ENDED onto AgentSession slot",
            exc_info=True,
        )


async def _resolve_attribution(
    storage_provider,
    session: WorkspaceSession,
) -> tuple[str | None, str | None]:
    """Return ``(workspace_name, session_label)`` for the attribution header.

    Loads the Workspace row to get its human-readable name. Falls back to
    ``workspace_id`` when the row is missing or has no name. Never raises.
    """
    workspace_name: str | None = None
    try:
        ws = await storage_provider.get_storage(Workspace).get(session.workspace_id)
        workspace_name = (ws.name if ws is not None else None) or session.workspace_id
    except Exception:
        workspace_name = session.workspace_id
    return workspace_name, session.id


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
