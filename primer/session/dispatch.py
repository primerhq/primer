"""Worker-side session-turn dispatch.

One ``run_one_session_turn`` invocation per claimed session lease.  The
worker pool calls this with the :class:`Lease` it received from the
:class:`ClaimEngine`; the function drives one full execution turn,
persists every :class:`StreamEvent` as a :class:`SessionMessageRecord`
to the workspace's ``messages.jsonl`` via :class:`WorkspaceMessageWriter`,
publishes a ``session:{sid}:tick`` event per record so the workspace tap
(and any other bus subscriber) sees new records in real time, honours cancel signals delivered over
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
from typing import Any, NamedTuple, Protocol, runtime_checkable
from collections.abc import Awaitable, Callable, Mapping, Sequence

from pydantic_core import to_jsonable_python

from primer.int.claim import (
    CLAIM_PRIORITY_RESUME, ClaimKind, Lease, ParkRequest, ReleaseOutcome,
)
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
import primer.observability.metrics as _metrics
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.except_ import (
    NetworkError,
    NotFoundError,
    PrimerError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
)
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.model.workspace import Workspace
from primer.model.workspace_session import (
    NON_ENDED_STATUSES,
    NON_ENDED_STATUSES_NOT_PAUSED,
    SessionMessageKind,
    SessionMessageRecord,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.turn_log import (
    TurnLogCancelled,
    TurnLogCompleted,
    TurnLogFailed,
    TurnLogPhase,
    TurnLogResumed,
    TurnLogStarted,
    TurnLogYielded,
)
from primer.model.yield_ import CANCEL_REASON_PREEMPTED, YIELD_KIND_PREFIXES, ToolWaitPark, YieldToWorker
from primer.session.autonomy import session_is_autonomous
from primer.session.enqueue import SessionWakeDeps
from primer.session.delegation import (
    DelegationRecorder,
    reset_delegation_sink,
    set_delegation_sink,
)
from primer.session.graph_end import graph_end_for
from primer.session.mutation_lock import IN_LOCK_IO_TIMEOUT_S, session_lifecycle_lock
from primer.session.pending_messages import realize_next_pending
from primer.session.turns import has_open_turn
from primer.session.persistence import (
    TurnInvariantError,
    WorkspaceIO,
    WorkspaceMessageWriter,
    WorkspaceWriteTimeout,
    _CoalesceState,
    _create_tool_call_task_idempotent,
    flush_partial_output,
    infer_agent_phase,
    materialize_pending_tool_wait_rows,
    stash_and_flush_tool_call_record,
    stash_graph_scoped_ids,
    translate_stream_event,
)
from primer.observability.turn_log_writer import (
    BoundedTurnLogWriter,
    NoopTurnLogWriter,
    TurnLogWriter,
    safe_append as _safe_turn_log,
    to_problem_details,
)


logger = logging.getLogger(__name__)


@runtime_checkable
class _NamesWhyItEndedTheTurn(Protocol):
    """An exception that says, in one code, why it ended the turn: ``TurnStreamFailure`` (the LLM stream itself
    failed) and ``ContextOverflowUnrecoverable`` (the prompt cannot fit). Dispatch records the code as the
    session's ``ended_detail``; any other exception leaves it unset."""

    @property
    def ended_detail_code(self) -> str: ...


#: The failure codes of a model call that leave an INTERACTIVE session resting instead of ending it (C-024, the lead's ruling 2026-10-09):
#: transport failures, after the llm layer's own retries are spent (5xx, a 429, a dropped connection, a stream that stalled or never opened,
#: a generation that ran out its total budget). Resting never retries by itself (only a ``claimable`` row is re-armed, and a failed turn leaves
#: ``idle``); it keeps the session open so the next send continues the same invocation. ``llm_stream_error`` is the code of a stream that died
#: WITHOUT one, most often a broken connection (the lead's second ruling, 2026-10-09). Everything else ends the session: a rejection the operator
#: has to fix (``auth_error``, ``bad_request``, ``model_not_found``, ``unsupported_content``, ``context_overflow_unrecoverable``), a code that IS
#: set and that nobody classified, and a turn that raised something that is not a model error (``turn_failed``).
_RESTING_FAILURE_CODES: frozenset[str] = frozenset({
    "server_error", "rate_limit", "network_error", "connect_timeout", "stream_timeout", "generation_timeout", "llm_stream_error",
})

#: The code of a RAISED transport error that carries none (``AggregatedLLM``'s exhausted-pool ``RateLimitError`` is one): its class says what it is.
_TRANSPORT_CODE_BY_CLASS: tuple[tuple[type[PrimerError], str], ...] = (
    (RateLimitError, "rate_limit"),
    (ServerError, "server_error"),
    (NetworkError, "network_error"),
    (ProviderTimeoutError, "stream_timeout"),
)

# How often a running turn re-reads its session row for a Stop whose bus message never
# arrived (see _cancel_watcher). A fallback, so it only has to be quick enough that a Stop
# does not feel lost: one point read per running session per interval, per worker. The
# first read is immediate, so a Stop recorded before the turn began is honoured at once.
_INTERRUPT_POLL_S = 2.0


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

    Cancellation contract: a cancellation delivered to the calling task WHILE the turn is in its cancelled
    exit is absorbed, consumed (``uncancel``) and the exit's outcome returned (see ``_finish_despite_cancel``),
    so the call swallows that cancel and returns normally. A caller must therefore not wrap this in
    ``asyncio.timeout`` or a ``TaskGroup`` and rely on the cancellation propagating out of it: the timeout
    would not fire its ``TimeoutError`` and the group would not see its child cancelled. The worker pool
    (``_run_engine_session``) is the production caller and relies on the return.
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
        # A lease that leaked through onto an already-ENDED row can still
        # carry a stale turn_status="running" from whatever crashed before
        # ever reaching this function's own cleanup - heal it here too so
        # it can't outlive the session it belonged to.
        if session.turn_status == "running":
            async with session_lifecycle_lock().acquire(session_id):
                await _clear_turn_running(session_storage, session_id)
        return ReleaseOutcome(success=True, drop_lease=True)
    if session.cancel_requested:
        session.status = SessionStatus.ENDED
        session.ended_reason = "cancelled"
        session.ended_at = _now()
        # Per the comment above, this branch is itself a crash-recovery
        # path (cancel_requested carried over from a process that died
        # mid-turn) - reset unconditionally, same rationale as
        # session_reconcile's workspace_lost transition.
        session.turn_status = "idle"
        session.turn_started_at = None
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
            await _clear_turn_running(session_storage, session_id)
        # Drop the lease but preserve the park columns: a paused session that
        # was 'resumable' keeps its marker + parked_state so a later /resume
        # re-arms the lease and replays the hook. preserve_park does NOT block
        # the turn_no bump: the adapter bumps on success in this branch too
        # (primer/claim/adapters/sessions.py), so this exit also applies a bump
        # that a rolled-back release of the previous turn lost.
        return ReleaseOutcome(
            success=True, drop_lease=True, preserve_park=True,
        )

    # ------------------------------------------------------------------
    # 1.4 A completed turn whose release never committed is not run again.
    # ------------------------------------------------------------------
    # Decided from a FRESH row under the lifecycle lock, never from the row
    # read above. Before the running flip and before build_executor: the
    # no-op path calls no model, flips nothing and builds nothing.
    noop = await _noop_if_turn_already_completed(deps, session_storage, session_id)
    if noop is not None:
        return noop

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
    # Same write also flips turn_status to "running" with a fresh
    # turn_started_at - unconditionally, not just when it was "claimable" -
    # since every path reaching this point is genuinely about to build an
    # executor and stream a turn. Before this, nothing in the codebase ever
    # wrote "running" (grep-confirmed): turn_status went idle -> claimable
    # -> idle, so REST clients polling it mid-turn always saw "idle" and had
    # no way to reconstruct a busy state after a refresh (live diagnosis,
    # task 01a04d64-b4ba). _clear_turn_running (below) is the matching
    # cleanup for every exit this turn can take.
    # The flip is ONE conditional, field-scoped write (patch_if of exactly the fields it owns, guarded on the row
    # not being PAUSED or ENDED): the old whole-document update of a second read wrote the stale status back over a
    # pause or an end that another process committed after the top read above (the exits decided from THAT read),
    # reverted a last_seq a steer had just written, and ran the turn anyway. When it is refused the row is judged
    # like the completed-turn guard judges it (gone: the vanished exit; ENDED: the ENDED exit; PAUSED: the pause
    # exit, park kept). The turn's writer is seeded from the row the flip WROTE, so the seq it starts after is the
    # one that was current at the write.
    _phase_stamp = _now()
    async with session_lifecycle_lock().acquire(session_id):
        flipped, refusal = await _flip_to_running(session_storage, session, _phase_stamp)
    if refusal is not None:
        return refusal
    seed_seq = flipped.last_seq

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
    except WorkspaceRefusedError as refused:
        # The deployment refuses this session's workspace (ticket 01a1072f). That is not a failure of the
        # session: its workspace is intact and usable once moved, so the turn fails and the session stays
        # resumable instead of ending ``failed``.
        return await pause_session_for_refused_workspace(session_storage, session_id, refused)
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
                # 01a06cbc: no executor was ever built (that's what just
                # raised), so the AgentSession-slot mirror has nothing to
                # unwrap via .session -- workspace_registry lets it fall
                # back to loading the slot independently instead.
                workspace_registry=deps.workspace_registry,
            )
            await _clear_interrupt_requested(session_storage, session_id)
            await _clear_turn_running(session_storage, session_id)
        return ReleaseOutcome(success=False, drop_lease=True)
    if executor is None:
        logger.warning("executor builder returned None for session %s", session_id)
        async with session_lifecycle_lock().acquire(session_id):
            await _clear_turn_running(session_storage, session_id)
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
    # Bounded: the production turn log writes over the same workspace connection as the message log, so on a dead connection every
    # entry (and its first-append bootstrap read) would hold the turn's exits, the park arms' first statement among them. The first
    # one that misses the best-effort bound breaks it; the rest are skipped at once.
    turn_log = BoundedTurnLogWriter(
        deps.turn_log_writer_factory(deps.workspace_io, session_id), timeout_s=_BEST_EFFORT_IO_TIMEOUT_S,
    )
    # A compaction marker the executor writes mid-turn takes its seq from this writer, so it cannot
    # collide with the events the writer buffers (an executor without the hook is left as it was).
    _bind_event_log = getattr(executor, "bind_event_log", None)
    if _bind_event_log is not None:
        _bind_event_log(writer)

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

    # agent_phase (01a04d91-a7a0): the row already reads "thinking" (set
    # alongside turn_status="running" at step 1.5, before build_executor);
    # publish the matching live tap frame + audit entry now that
    # deps.event_bus/turn_log both exist. _agent_phase tracks the LOCAL
    # notion of "what we last wrote" so the streaming loop below only acts
    # on genuine transitions (infer_agent_phase can return the same value
    # many times in a row - e.g. every ReasoningDelta - and only the first
    # one after a change should trigger a write/publish/log).
    _agent_phase = "thinking"
    from primer.tap.delta import publish_phase_frame
    if deps.event_bus is not None:
        await publish_phase_frame(
            deps.event_bus.publish, session_id=session_id,
            phase=_agent_phase, turn_no=session.turn_no,
        )
    await _safe_turn_log(turn_log, TurnLogPhase(
        seq=0, ts=_now(), turn_no=session.turn_no, phase=_agent_phase,
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
    # The cancellations this task already carried when the turn began (an outer scope may have absorbed one without uncancel()):
    # a hard Cancel absorbed below consumes only what arrived after.
    _task_now = asyncio.current_task()
    _entered_cancelling = _task_now.cancelling() if _task_now is not None else 0

    cancel_event = asyncio.Event()
    cancel_task = asyncio.create_task(
        _cancel_watcher(
            deps.event_bus, session_id, cancel_event, session_storage=session_storage,
        ),
        name=f"sess-cancel-{session_id}",
    )
    # An executor that takes the Stop event (the agent executors) races it against every wait
    # for the model and decides where the turn stops, so a Stop reaches a model that has not
    # produced its first token and a tool batch's results are never cut short. Dispatch then
    # reads ``was_interrupted`` once the stream ends instead of breaking out on its own.
    # Executors without it (graph sessions, test fakes) keep the cooperative break below.
    _bind_interrupt = getattr(executor, "bind_interrupt_event", None)
    executor_owns_interrupt = _bind_interrupt is not None
    if _bind_interrupt is not None:
        _bind_interrupt(cancel_event)

    # ------------------------------------------------------------------
    # 4. Stream events from executor
    # ------------------------------------------------------------------
    coalesce_state = _CoalesceState()

    # 01a0518b (seam-split summit): bind the per-turn scoped-call resolver
    # now that coalesce_state exists - this is why the resolver can't be
    # constructor-injected like turn_no/artifact_storage/the flag (see
    # primer.agent.base._BaseAgentExecutor's own comment on
    # self._resolve_scoped_call). Defensive getattr: only the agent
    # executor has this method (chat/workspace surface, boundary (c)) - a
    # graph session's executor has no such attribute and this is a no-op
    # for it, exactly as if tool_calls_as_claims_enabled were never
    # threaded there at all.
    _bind_resolver = getattr(executor, "bind_scoped_call_resolver", None)
    if _bind_resolver is not None:
        _bind_resolver(_make_scoped_call_resolver(coalesce_state))

    # 01a0518b (graph-surface boundary d): the graph executor needs the
    # RAW coalesce_state, not a finished resolver - _agent_node.py's mixin
    # builds a NODE-QUALIFIED resolver fresh at each node dispatch (the
    # (None, raw_id) keying above is chat/workspace-only; the graph
    # surface's coalesce_state.scoped_call_ids is keyed (node_id, raw_id)
    # since concurrent fan-out siblings reuse raw provider ids). Separate
    # defensive getattr, same reasoning: only the graph executor has this
    # method, so an agent session's executor is unaffected.
    _bind_coalesce = getattr(executor, "bind_coalesce_state", None)
    if _bind_coalesce is not None:
        _bind_coalesce(coalesce_state)

    # Subagent runs execute inline in this turn with no writer of
    # their own, so the recorder is published here and picked up by
    # the invoke loops through a contextvar. Without it a delegated
    # run leaves only an opaque tool call in the transcript.
    _delegation_token = set_delegation_sink(DelegationRecorder(
        writer=writer, event_bus=deps.event_bus, session_id=session_id,
        turn_no=session.turn_no,
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

    # The failure exit of the turn below: an ERROR record, ENDED/failed, the terminal publish and the checkpoint
    # hooks. Shared by the catch-all ``except Exception`` and the park arms, whose own TurnInvariantError (a
    # deterministic bookkeeping break while parking) is raised inside an except handler the catch-all never sees.
    # Output the model had already streamed is still only in the coalesce buffers when a turn ends without reaching a tool call or
    # Done: it becomes a record at neither. A Stop or Cancel makes it durable (the finally below) and so does a FAILED turn
    # (_end_turn_failed), ahead of the terminal record that explains why the answer ends there; otherwise it existed only in the live
    # view and vanished on refresh. Best effort and bounded: the appends can run the writer's age flush of records already buffered,
    # which goes over the workspace connection, and the terminal exit waits on this. A lost lease never calls it (the session may
    # belong to another worker now).
    async def _make_partial_output_durable(what: str, then: str) -> None:
        try:
            async with asyncio.timeout(_BEST_EFFORT_IO_TIMEOUT_S):
                for partial in flush_partial_output(
                    coalesce_state, delta_sink=delta_buffer, turn_no=session.turn_no,
                ):
                    await writer.append(partial)
        except TimeoutError:
            logger.warning(
                "session %s: the output streamed before the %s was not confirmed within %gs (the "
                "workspace is not accepting writes); %s",
                session_id, what, _BEST_EFFORT_IO_TIMEOUT_S, then,
            )
        except Exception:  # noqa: BLE001 - the terminal exit must still land
            logger.exception(
                "session %s: could not persist the output streamed before the %s", session_id, what,
            )

    async def _end_turn_failed(exc: BaseException) -> ReleaseOutcome:
        # Build the ProblemDetails envelope once and reuse it for BOTH
        # the structured turn-log event and the messages.jsonl ERROR
        # record. Operators looking at the Messages tab now see the
        # real exception type/title/detail (matching what the Turn log
        # tab shows) instead of the legacy "unexpected executor error"
        # generic string. Spec §6.1 called for the legacy string to go
        # away once the turn-log existed; this is that cutover.
        problem = to_problem_details(exc)
        # to_problem_details logged the traceback under this error_id; the
        # record served to session readers carries only the id.
        logger.error(
            "session %s executor raised unexpected error (error_id=%s);"
            " releasing claim",
            session_id, (problem.extensions or {}).get("error_id"),
        )
        # The answer that was streaming when the turn died goes into the log BEFORE the ERROR record below.
        await _make_partial_output_durable("failure", "ending the turn without it")
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
            await writer.append(error_rec)
            await _flush_and_tick(deps, writer, session_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "session %s failed to write error record after executor"
                " failure; session will still be transitioned to ENDED",
                session_id,
            )
        failure_code = _failure_code(exc)
        # C-024: a transport failure of the model call leaves an interactive session RESTING (WAITING, no ended_reason); the row still says
        # the turn failed (last_turn_error), so the next send continues the same invocation. A graph, trigger or webhook one-shot has no human
        # to resume it (a resting one-shot would hold a parallelism="skip" gate shut forever), so it ends as before: a graph binding is
        # autonomous whatever an explicit ``autonomous=False`` says.
        may_rest = failure_code in _RESTING_FAILURE_CODES and not (
            isinstance(session.binding, GraphSessionBinding) or session_is_autonomous(session)
        )
        async with session_lifecycle_lock().acquire(session_id):
            # BEFORE the status moves: a reader that sees the row after the transition must see why (C-024).
            stamped = await _record_last_turn_error(session_storage, session_id, failure_code, session.binding_epoch)
            # Without its stamp a rested first-turn failure has every mark of a session that never started, so it ends. A Cancel that lands
            # (on any process: the lock above is this one's) is not lost either: the rest is a conditional write that refuses a row with
            # ``cancel_requested`` set, and then the session ends, as the clean-completion arm ends one.
            rested = may_rest and stamped and await _rest_session(session_storage, session, expected_epoch=session.binding_epoch)
            if rested:
                written = _TerminalWrite(True, SessionStatus.WAITING, None)
                # The executor ended the on-disk slot when the turn failed. The row now says the session is alive, so the slot must too, or the
                # next send (or a trigger's append, or the follow-up realized below) meets an ENDED slot and is refused.
                await _reopen_agent_session_slot(executor)
            else:
                written = await _transition_session_status(
                    session_storage,
                    session,
                    new_status=SessionStatus.ENDED,
                    ended_reason="failed",
                    # 01a070d6: a TurnStreamFailure means the LLM stream itself
                    # is why the turn failed - ended_detail_code always resolves
                    # to something usable (a real classifier code, or its own
                    # fallback), so monitoring can tell "the LLM was
                    # unreachable" apart from "some other internal error"
                    # instead of both reading as an undifferentiated "failed".
                    ended_detail=exc.ended_detail_code if isinstance(exc, _NamesWhyItEndedTheTurn) else None,
                    executor=executor,
                    expected_epoch=session.binding_epoch,
                )
            await _clear_interrupt_requested(session_storage, session_id)
            await _persist_last_seq(session_storage, session_id, writer.last_seq)
            await _advance_drain_cursor(session_storage, session_id)
        # Every failed turn is announced, ended or resting: ``session.ended`` is only for an ended one, and a session that rests after a
        # transport failure would otherwise fail without a word on the event log (C-024). First, so a consumer that reacts to
        # ``session.ended`` already has the failure that ended it.
        await _event_recorder(deps).emit(
            "session.turn_failed",
            workspace_id=session.workspace_id,
            session_id=session_id,
            payload={"code": failure_code, "ended": written.status == SessionStatus.ENDED},
        )
        # What the ROW says: if the session was ended by something else while the turn failed, that is
        # the reason the event log must carry (the turn's own failure is still counted below).
        await _publish_terminal(deps, session, written.status, written.ended_reason)
        await turn_log.aclose()
        await _apply_pending_switch_at_checkpoint(deps, session)
        await _realize_pending_at_checkpoint(deps, session)
        _observe_turn(session, "failed", _turn_started_at)
        return ReleaseOutcome(success=False, drop_lease=True)

    # 01a0692f: held so the finally below can explicitly aclose() it on
    # every exit path (break, exception, or normal exhaustion - aclose()
    # on an already-finished generator is a no-op). A bare `break` alone
    # leaves the generator suspended mid-yield; for a graph session that
    # generator is `_run_superstep_loop`, whose own node-task-cancellation
    # cleanup lives in ITS `finally`, reached only once this generator is
    # actually closed. Without an explicit aclose(), Python still closes
    # it eventually via the asyncgen finalizer hook when this local's
    # refcount drops - but that runs on a LATER event-loop tick, not
    # in-line, so a mid-turn cancel on a wide fan-out could leave sibling
    # node tasks running in the background for a tick or few.
    turn_events = executor.invoke([])
    try:
        async for event in turn_events:
            # agent_phase (01a04d91-a7a0): inspect the RAW event, before
            # translate_stream_event's coalescing, so "responding"/
            # "executing" are true the instant the tokens/tool call start
            # arriving rather than only once a buffer later flushes. Only
            # acts on an actual change - infer_agent_phase legitimately
            # returns the same value on many consecutive events (every
            # ReasoningDelta while still "thinking", say).
            _new_phase = infer_agent_phase(event)
            if _new_phase is not None and _new_phase != _agent_phase:
                _agent_phase = _new_phase
                await _write_agent_phase(
                    session_storage, session_id, session.turn_no, _agent_phase,
                )
                if deps.event_bus is not None:
                    await publish_phase_frame(
                        deps.event_bus.publish, session_id=session_id,
                        phase=_agent_phase, turn_no=session.turn_no,
                    )
                await _safe_turn_log(turn_log, TurnLogPhase(
                    seq=0, ts=_now(), turn_no=session.turn_no,
                    phase=_agent_phase,
                ))

            # Translate StreamEvent → SessionMessageRecord(s)
            result = translate_stream_event(
                event, coalesce_state, delta_sink=delta_buffer,
                turn_no=session.turn_no,
            )
            if result is None:
                # Check cancel between events even when nothing was produced
                if cancel_event.is_set() and not executor_owns_interrupt:
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
                # 7a gate review (verdict item 3): shared with the graph
                # resume drain's own tap (_ResumeDrainTap.observe) so the
                # two never drift into independently-maintained copies of
                # the same seam - see the helper's own docstring for the
                # full stash/flush reasoning and item A's gating rationale.
                await stash_and_flush_tool_call_record(
                    rec, seq, coalesce_state=coalesce_state, writer=writer,
                    tool_calls_as_claims_enabled=getattr(
                        executor, "_tool_calls_as_claims_enabled", False,
                    ),
                )
                await deps.event_bus.publish(
                    f"session:{session_id}:tick", {"seq": seq}
                )
                if rec.kind == SessionMessageKind.GRAPH_TRANSITION:
                    await _emit_graph_transition(deps, session, rec)

            # Honour cancel after processing the current batch
            if cancel_event.is_set() and not executor_owns_interrupt:
                cancel_requested = True
                break

        # The stream ended on its own. If the executor ended it because the Stop fired, the
        # turn was interrupted: take the soft exit below, not the clean-completion path.
        if executor_owns_interrupt and getattr(executor, "was_interrupted", False):
            cancel_requested = True

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
        # agent_phase: an explicit "waiting" transition at the moment of
        # park (rather than relying solely on the finally block's
        # reset-to-None below), so a live client / the turns.jsonl audit
        # sees the same "the agent stopped actively working" signal a
        # clean completion's Done event already produces via
        # infer_agent_phase - YieldToWorker is a Python exception, never
        # a StreamEvent, so infer_agent_phase never sees it.
        if _agent_phase != "waiting":
            _agent_phase = "waiting"
            await _write_agent_phase(
                session_storage, session_id, session.turn_no, _agent_phase,
            )
            if deps.event_bus is not None:
                await publish_phase_frame(
                    deps.event_bus.publish, session_id=session_id,
                    phase=_agent_phase, turn_no=session.turn_no,
                )
            await _safe_turn_log(turn_log, TurnLogPhase(
                seq=0, ts=_now(), turn_no=session.turn_no,
                phase=_agent_phase,
            ))
        rec = _yielded_record(park)
        await _flush_and_tick(deps, writer, session_id, rec)
        await _event_recorder(deps).emit(
            "session.parked",
            workspace_id=session.workspace_id,
            session_id=session_id,
            payload={"event_key": park.yielded.event_key},
        )

        yielded = park.yielded
        parked_at = _now()
        # Per-yield timeout takes precedence; fall back to the global yield
        # cap (60 min default).
        timeout = yielded.timeout if yielded.timeout is not None else 3600.0
        parked_until = parked_at + timedelta(seconds=timeout)

        # 01a068ea-dc95: the durable TOOL_CALL SessionMessageRecord this
        # park will answer was minted with the SCOPED id (persistence.py's
        # ToolCallStart handler), not park.tool_call_id (the raw provider
        # id LLM-context reconstruction needs). coalesce_state processed
        # this call's ToolCallEnd earlier in the same async-for loop above
        # (streaming events arrive strictly before the tool dispatch that
        # might yield), and no _ExecutorToolResult ever popped the mapping
        # since this call yielded instead of returning -- so the scoped id
        # is still sitting in scoped_call_ids right now. node_id is always
        # None here: this turn driver (run_one_session_turn) never passes
        # one to translate_stream_event. Stash it so the resume coordinator
        # can pair the eventual TOOL_RESULT display record correctly
        # instead of falling back to the raw id.
        scoped_tool_call_id = coalesce_state.scoped_call_ids.get(
            (None, park.tool_call_id)
        )

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

        # 01a0690a: stash each pending entry's scoped tool-call id (the id
        # the durable TOOL_CALL record actually carries, minted by THIS
        # turn's coalesce_state via the live path's _GraphNodeEvent unwrap)
        # into graph_checkpoint, plus the per-node mint-seq snapshot the
        # eventual resume drain needs to avoid re-minting colliding ids.
        # {}/None for an agent-bound park (graph_checkpoint is None).
        node_tool_call_seq = stash_graph_scoped_ids(graph_checkpoint, coalesce_state)

        # 01a0518b (mixed-park wake seam): a co-pending tool_wait batch
        # (one or more graph nodes independently dispatched claims in the
        # SAME superstep as this gate) rides along in graph_checkpoint -
        # materialize its ToolCallTask rows here, exactly like the pure
        # except-ToolWaitPark branch below does, and extend this park's
        # OWN event_keys so durably_mark_session_resumable's multi-event
        # accumulation recognizes each batch's wake key too. {} for an
        # agent-bound park or a graph park with no co-pending tool_wait -
        # a no-op, byte-identical to before this arc.
        # A deterministic bookkeeping break here ends the turn failed (a retry would hit it again); the turn log is
        # closed only after this, so that failure is recorded in it too.
        try:
            extra_wake_keys = await materialize_pending_tool_wait_rows(
                deps.storage_provider, deps.claim_engine, session.id, session.turn_no,
                coalesce_state, parked_at,
                (graph_checkpoint or {}).get("pending_tool_waits") or [],
            )
        except TurnInvariantError as exc:
            return await _end_turn_failed(exc)
        await turn_log.aclose()

        # Forward the prompt to every channel associated with this
        # session's workspace (ask_user / tool_approval gates). Only once
        # the co-pending batch above is materialized: a TurnInvariantError
        # there ends the session, and an ENDED session must not have asked
        # a human anything. Awaited so delivery is attempted before the
        # lease drops;
        # the fan-out is BEST EFFORT: _dispatch_to_channels never raises (an
        # envelope that cannot be built, an unreadable ask_user file or a
        # dispatcher error is logged at ERROR and the park carries on; see
        # its docstring) and no-ops when no dispatcher is wired. Function-local import mirrors the ParkedState import
        # above to avoid the worker->dispatch circular import.
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
            scoped_tool_call_id=scoped_tool_call_id,
            node_tool_call_seq=node_tool_call_seq or None,
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
        # 01a0518b: fold any co-pending tool_wait batches' own wake keys
        # into event_keys so the multi-event accumulation mechanism
        # recognizes them too - see the materialization step above. An
        # existing single-key park (no event_keys at all) gains a real
        # multi-event list the FIRST time this fires, which is correct:
        # a tool_wait batch existing at all means there is now genuinely
        # more than one thing this session can wake on.
        combined_event_keys = list(getattr(yielded, "event_keys", None) or [])
        combined_event_keys.extend(extra_wake_keys)
        return ReleaseOutcome(
            success=True,
            drop_lease=True,
            park=ParkRequest(
                parked_state=parked_state.to_jsonable(),
                parked_event_key=yielded.event_key,
                parked_event_keys=combined_event_keys or None,
                parked_until=parked_until,
                parked_at=parked_at,
            ),
        )

    except ToolWaitPark as tool_wait:
        # ------------------------------------------------------------------
        # 5b. Tool-wait batch park (Phase 3 stage 7a, 01a0518b) - the seam
        # split turned this turn's tool-call batch into independently-
        # claimable ToolCallTask rows instead of running them in-process.
        # Mirrors the YieldToWorker branch above at BATCH granularity:
        # write the turn-log + "waiting" phase signal, persist one
        # ToolCallTask row per outstanding/notifying call, write the
        # tool_wait-shaped parked_state, and return a park outcome. No
        # channel dispatch here - a tool_wait park is not a human-facing
        # gate itself (an individual task going GATED later is handled by
        # ToolCallClaimAdapter.on_release, task-granular, not here).
        # ------------------------------------------------------------------
        from primer.model.tool_call_task import (
            ToolCallTask,
            ToolCallTaskState,
            tool_call_task_id,
        )
        from primer.session.yields import tool_wait_event_key_or_none
        from primer.worker.yield_runtime import ToolWaitParkedState

        # The park exception carries SCOPED call ids (unique within one session only; the transcript records are
        # keyed on them). A task row id is a global primary key and a lease key, so every row, lease, batch list and
        # park blob below uses the session-qualified id; the record lookups stay on the scoped one.
        outstanding_ids = [
            tool_call_task_id(session_id, scoped_id)
            for scoped_id in tool_wait.outstanding_task_ids
        ]
        notifying_ids = [
            tool_call_task_id(session_id, scoped_id)
            for scoped_id, _ in tool_wait.notifying_results
        ]
        all_batch_ids = [*outstanding_ids, *notifying_ids]

        await _safe_turn_log(turn_log, TurnLogYielded(
            seq=0,
            ts=_now(),
            turn_no=session.turn_no,
            yield_kind="tool_wait",
            event_key=tool_wait.event_key,
        ))
        if _agent_phase != "waiting":
            _agent_phase = "waiting"
            await _write_agent_phase(
                session_storage, session_id, session.turn_no, _agent_phase,
            )
            if deps.event_bus is not None:
                await publish_phase_frame(
                    deps.event_bus.publish, session_id=session_id,
                    phase=_agent_phase, turn_no=session.turn_no,
                )
            await _safe_turn_log(turn_log, TurnLogPhase(
                seq=0, ts=_now(), turn_no=session.turn_no,
                phase=_agent_phase,
            ))

        rec = _tool_wait_yielded_record(tool_wait)
        await _flush_and_tick(deps, writer, session_id, rec)
        await _event_recorder(deps).emit(
            "session.parked",
            workspace_id=session.workspace_id,
            session_id=session_id,
            payload={
                "event_key": tool_wait.event_key,
                "outstanding_task_count": len(tool_wait.outstanding_task_ids),
                "notifying_task_count": len(tool_wait.notifying_results),
            },
        )

        parked_at = _now()
        # No per-batch timeout sentinel exists yet on ToolWaitPark - fall
        # back to the same global yield cap the YieldToWorker branch uses.
        timeout = 3600.0
        parked_until = parked_at + timedelta(seconds=timeout)

        # 01a0518b (graph-surface boundary d): a graph-bound park carries
        # its own executor snapshot (see _build_pending_tool_wait_park) -
        # when present, row creation reads the PER-NODE breakdown from
        # graph_checkpoint['pending_tool_waits'] via the SAME shared
        # helper the mixed (except-YieldToWorker) branch uses, rather
        # than tool_wait's own flattened fields, which would otherwise
        # combine every node's batch_task_ids/wake key into one - see
        # materialize_pending_tool_wait_rows' own docstring. An
        # agent-bound park (graph_checkpoint is None) keeps the original
        # flat-field loop unchanged.
        graph_checkpoint = getattr(tool_wait, "graph_checkpoint", None)
        node_tool_call_seq: dict[str, int] | None = None
        # The same failure exit as the mixed arm above, for the same deterministic breaks.
        try:
            if graph_checkpoint is not None:
                per_node_wake_keys = await materialize_pending_tool_wait_rows(
                    deps.storage_provider, deps.claim_engine, session.id,
                    session.turn_no, coalesce_state, parked_at,
                    graph_checkpoint.get("pending_tool_waits") or [],
                )
                # 01a0518b boundary d: mirrors the mixed (except-YieldToWorker)
                # branch's own stash - a resumed node that dispatches a
                # FURTHER tool_calls_as_claims round before finishing must not
                # re-mint a scoped id THIS turn already used (see
                # ToolWaitParkedState.node_tool_call_seq's own docstring).
                node_tool_call_seq = stash_graph_scoped_ids(
                    graph_checkpoint, coalesce_state,
                )
            else:
                per_node_wake_keys = []
                task_storage = deps.storage_provider.get_storage(ToolCallTask)
                for scoped_id in tool_wait.outstanding_task_ids:
                    record_seq = coalesce_state.tool_call_record_seq.get(scoped_id)
                    tool_name = coalesce_state.tool_call_record_name.get(scoped_id)
                    if record_seq is None or tool_name is None:
                        raise TurnInvariantError(
                            f"session {session_id} ToolWaitPark outstanding task "
                            f"{scoped_id!r} has no matching TOOL_CALL record in "
                            "this turn's coalesce_state - the durable-append-"
                            "before-claimable invariant broke"
                        )
                    task_id = tool_call_task_id(session_id, scoped_id)
                    await _create_tool_call_task_idempotent(
                        task_storage,
                        ToolCallTask(
                            id=task_id,
                            session_id=session_id,
                            turn_no=session.turn_no,
                            tool_name=tool_name,
                            state=ToolCallTaskState.QUEUED,
                            record_seq=record_seq,
                            call_id=tool_wait.call_ids.get(scoped_id),
                            created_at=parked_at,
                            batch_task_ids=all_batch_ids,
                        ),
                        session_id=session_id,
                    )
                    if deps.claim_engine is not None:
                        # Priority 50, not the fresh-work default 100: a tool call is a continuation of a
                        # turn a human is waiting on, and must not queue behind fresh sessions.
                        await deps.claim_engine.upsert(
                            ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME,
                        )
                for scoped_id, result in tool_wait.notifying_results:
                    record_seq = coalesce_state.tool_call_record_seq.get(scoped_id)
                    tool_name = coalesce_state.tool_call_record_name.get(scoped_id)
                    if record_seq is None or tool_name is None:
                        raise TurnInvariantError(
                            f"session {session_id} ToolWaitPark notifying result "
                            f"{scoped_id!r} has no matching TOOL_CALL record in "
                            "this turn's coalesce_state - the durable-append-"
                            "before-claimable invariant broke"
                        )
                    await _create_tool_call_task_idempotent(
                        task_storage,
                        ToolCallTask(
                            id=tool_call_task_id(session_id, scoped_id),
                            session_id=session_id,
                            turn_no=session.turn_no,
                            tool_name=tool_name,
                            state=ToolCallTaskState.DONE,
                            record_seq=record_seq,
                            call_id=tool_wait.call_ids.get(scoped_id),
                            created_at=parked_at,
                            finished_at=parked_at,
                            result_state=result.model_dump(mode="json"),
                            batch_task_ids=all_batch_ids,
                        ),
                        session_id=session_id,
                    )
            # 01a0518b (mixed-park wake seam review): the FUNCTIONAL wake key, a pure function of a task id of the
            # batch (see tool_wait_event_key). The batch's ids are stamped on every row as batch_task_ids (the key
            # itself is not stored), so ToolCallClaimAdapter.on_release's last-sibling branch recomputes it with no
            # session-row read. Distinct from tool_wait.event_key (observability-only: the turn log and the audit
            # emit above, never parked_event_key). A graph park keys on its first batch that parses (the
            # materializer drops a malformed one); a park with no wake key at all is never written: it would have
            # no parked_event_key, hence no timeout backstop either.
            if graph_checkpoint is not None:
                wake_key = per_node_wake_keys[0] if per_node_wake_keys else None
            else:
                wake_key = tool_wait_event_key_or_none(session_id, scoped_task_id=all_batch_ids[0], site="dispatch")
            if wake_key is None:
                raise TurnInvariantError(
                    f"session {session_id} tool_wait park has no wake key: no batch's task id parses as a "
                    "scoped tool-call id, and a park nothing can wake is never written"
                )
        except TurnInvariantError as exc:
            return await _end_turn_failed(exc)
        await turn_log.aclose()

        notifying_task_ids = list(notifying_ids)
        captured_messages = tool_wait.llm_messages or []
        llm_message_dicts = [m.model_dump(mode="json") for m in captured_messages]

        parked_state = ToolWaitParkedState(
            outstanding_task_ids=list(outstanding_ids),
            notifying_task_ids=notifying_task_ids,
            event_key=wake_key,
            llm_messages=llm_message_dicts,
            turn_no=session.turn_no,
            started_at=_turn_started_at,
            graph_checkpoint=graph_checkpoint,
            node_tool_call_seq=node_tool_call_seq,
        )

        logger.info(
            "session %s parking on tool_wait batch (%d claimable, %d "
            "notifying, wake_key=%r)",
            session_id, len(tool_wait.outstanding_task_ids),
            len(notifying_task_ids), wake_key,
        )

        async with session_lifecycle_lock().acquire(session_id):
            await _clear_interrupt_requested(session_storage, session_id)
            await _persist_last_seq(session_storage, session_id, writer.last_seq)

        _observe_turn(session, "parked", _turn_started_at)
        return ReleaseOutcome(
            success=True,
            drop_lease=True,
            park=ParkRequest(
                parked_state=parked_state.to_jsonable(),
                parked_event_key=wake_key,
                parked_event_keys=per_node_wake_keys or None,
                parked_until=parked_until,
                parked_at=parked_at,
            ),
        )

    except asyncio.CancelledError as preempt:
        # The pool hard-cancelled this task wherever it was awaiting. A user Cancel reaches a turn blocked in a long model call
        # (one that never yields the event loop's cancel_event a look) this way, via ``_cancel_loop`` / the row reconciler's
        # ``cancel_once``. It used to leave here without the cancelled exit: the pool's convergence ended the row, but no
        # CANCELLED record, tick, terminal event, turn-log entry or metric was written and the turn's streamed text was lost, so no
        # client ever heard the turn had ended (console review 2026-10-08, C-011).
        #
        # Three things tell a user Cancel from the other causes of this exception, and only a Cancel is this turn's to land:
        # * the REASON: a lost lease (``CANCEL_REASON_PREEMPTED``, from the heartbeat) means the session may belong to another worker
        #   now, so this execution must not write to it on its way out (the rule ``agent/base.py`` applies to its own cleanup). It
        #   propagates even when the row is flagged, and the pool's convergence handles it as before. A drain timeout
        #   (``worker_drain_timeout``) is different: this worker still holds the lease, so a flagged row is landed;
        # * the ROW, as the pool's convergence reads it: ``cancel_requested`` set and the row not ended (a force-deleted row is
        #   already ENDED and is left to the delete);
        # * a row that cannot be read decides nothing, and must not turn the cancellation into its own error.
        if preempt.args[:1] == (CANCEL_REASON_PREEMPTED,):
            raise preempt
        try:
            is_user_cancel = await _row_holds_a_cancel(session_storage, session_id)
        except Exception:  # noqa: BLE001 - an unreadable row must not turn the cancellation into its own error
            logger.warning(
                "session %s: could not read the row to tell a Cancel from a lost lease; propagating the cancellation",
                session_id, exc_info=True,
            )
            is_user_cancel = False
        if not is_user_cancel:
            raise preempt
        # Make the streamed-but-unrecorded output durable in the cleanup below, then take the one exit after it. The cancellation is
        # consumed DELIBERATELY, down to what the task carried on entry, so that code which checks ``cancelling()`` afterwards
        # (``_finish_despite_cancel`` takes its own entry count from it, and whoever awaits this task sees the count it had before
        # the cancel was absorbed) does not read a cancel that was already handled as one still pending. A later preempt (a second
        # ``cancel_once`` from the reconciler, the drain timeout) is absorbed by ``_finish_despite_cancel`` around the exit, which
        # runs it as its own task. The exit's outcome is returned rather than the cancellation re-raised, as for every other
        # cancelled exit: a re-raise would drop it, the pool's convergence skips a row that is already ENDED, and ``on_release``
        # would write a terminal ERROR record.
        cancel_requested = True
        if _task_now is not None:
            while _task_now.cancelling() > _entered_cancelling:
                _task_now.uncancel()
        logger.info("session %s: a Cancel preempted the stream; landing the cancelled exit", session_id)

    except Exception as exc:
        return await _end_turn_failed(exc)

    finally:
        # 01a0692f: close the turn's event stream explicitly and first -
        # a graph session's generator chain (_run_superstep_loop) cancels
        # any still-in-flight fan-out node tasks in ITS OWN finally, which
        # only runs once this aclose() actually reaches it. See turn_events'
        # own comment above for why relying on implicit GC-driven closure
        # instead would only delay that cleanup, not skip it.
        try:
            await turn_events.aclose()
        except Exception:  # noqa: BLE001 - best-effort, never blocks release
            logger.exception(
                "session %s: turn_events.aclose() raised during cleanup",
                session_id,
            )
        if cancel_requested:
            # Output the model had already streamed when the Stop/Cancel landed is still only in the
            # coalesce buffers (it becomes a record at a tool call or at Done). Make it durable now,
            # ahead of the CANCELLED record below, or it exists only in the live view and vanishes
            # on refresh. Before delta_buffer.aclose() so the live parts are closed too.
            await _make_partial_output_durable("stop", "finishing the cancel without it")
        _metrics.sessions_active.labels(session.workspace_id).dec()
        reset_delegation_sink(_delegation_token)
        cancel_task.cancel()
        try:
            await cancel_task
        except (asyncio.CancelledError, Exception):
            pass
        if delta_buffer is not None:
            await delta_buffer.aclose()
        # The one chokepoint this comment already promises (park, error,
        # cancel and clean exits all run this finally) - reuse it to clear
        # turn_status back to "idle" for all four, rather than repeating
        # the clear at each of their own separate terminal-transition lock
        # blocks below. _clear_turn_running no-ops if turn_status isn't
        # "running" (e.g. a wake_session() raced in "claimable" during this
        # turn's cleanup), so it can never stomp a fresh claim.
        async with session_lifecycle_lock().acquire(session_id):
            await _clear_turn_running(session_storage, session_id)

    # A Cancel that landed AFTER the stream's last event (the model's terminal event is in, the
    # executor ended normally) was never seen by the loops above: an executor that owns the Stop
    # event does not have dispatch break on a set event, and none of them was waiting. Left alone
    # the turn would complete WAITING with cancel_requested still set ("I cancelled it and nothing
    # happened"). The row is the truth, so ask it before taking the clean-completion path.
    if not cancel_requested:
        late = await session_storage.get(session_id)
        if late is not None and late.cancel_requested and late.status != SessionStatus.ENDED:
            cancel_requested = True

    # ------------------------------------------------------------------
    # 5b. Cancel path — write CANCELLED record, transition row to ENDED
    # ------------------------------------------------------------------
    if cancel_requested:
        return await _finish_despite_cancel(_land_cancelled_turn(
            deps, session, executor=executor, writer=writer, turn_log=turn_log,
            started_at=_turn_started_at,
        ))

    # ------------------------------------------------------------------
    # 6. Clean completion: an agent turn's DONE record is written by
    #    translate_stream_event; a GRAPH run's end record (a node-less done,
    #    primer.session.graph_end) is appended here. Then flush, final tick,
    #    then transition the scheduler-visible row based on what the
    #    executor did.
    # ------------------------------------------------------------------
    # A graph run's stream ends with the End node's output: none of its records ends the graph's turn (they all carry a node_id), so the graph's own end
    # is appended here, after everything the stream wrote and flushed with it, as the one terminal of the turn (primer.session.graph_end). An agent turn has none.
    last_done_reason = getattr(executor, "last_done_reason", None)
    await _flush_and_tick(deps, writer, session_id, graph_end_for(last_done_reason, executor))

    agent_status = await _read_agent_session_status(executor)
    new_status, ended_reason = _post_turn_status(
        last_done_reason, agent_status,
        autonomous=session_is_autonomous(session),
    )
    # _post_turn_status returns ended_reason "completed", "failed" or None
    # (dispatch.py:833-842: max_tokens / content_filter park the session in
    # WAITING with no ended_reason). Only "failed" is a failure; a None
    # reason means the turn ran to a clean stop the session can continue
    # from, so it counts as completed. (Observed after the lock below: a
    # Cancel can still turn this turn into a cancelled one.)
    # Serialize the terminal transition + interrupt-flag clear against a
    # concurrent resume/pause/cancel/interrupt API call (T0432-style lost
    # update; see primer.session.mutation_lock). A clean completion can
    # still carry a stale interrupt_requested (e.g. the interrupt fired
    # too late to be observed by this turn's cancel_event check), so clear
    # it here too -- every terminal path must, or it leaks into a future
    # turn and can downgrade a later genuine Cancel to a Stop.
    late_cancel = False
    written: _TerminalWrite | None = None
    async with session_lifecycle_lock().acquire(session_id):
        # The Cancel route takes this same lock, so the row read here cannot be stale: a Cancel that
        # landed in the window since the check above wins over whatever the turn's own stop reason
        # mapped to (a cancelled session must not come to rest WAITING).
        locked = await session_storage.get(session_id)
        if locked is not None and locked.cancel_requested and locked.status != SessionStatus.ENDED:
            # A cancelled turn, not a completed one with a different status: it takes the SAME exit as
            # every other cancelled turn (the CANCELLED record, the cancelled metric, no reply event, no
            # relay), which re-decides and writes inside its own acquisition of this lock below.
            late_cancel = True
        else:
            written = await _transition_session_status(
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
            # Last write of the block: present means the cursor already advanced. Not for a row another path
            # ended meanwhile (the turn's own write was skipped).
            if not _ended_by_another_path(written, new_status, ended_reason):
                await _mark_turn_completed(session_storage, session_id, session.turn_no)

    if late_cancel:
        return await _finish_despite_cancel(_land_cancelled_turn(
            deps, session, executor=executor, writer=writer, turn_log=turn_log,
            started_at=_turn_started_at,
        ))

    assert written is not None   # the only other branch above returned
    # The row was ended by something else while the turn finished (a force-delete, the pool's preempt
    # convergence, the reconciler) and the write was skipped: announce what the ROW says, so the durable
    # event log does not contradict it, and relay no answer for a session that was ended under the turn.
    overridden = _ended_by_another_path(written, new_status, ended_reason)
    # A turn the agent's max_tool_turns stopped is counted under its own status: it is neither a normal completion
    # (WAITING/None interactive, ENDED/tool_turn_cap autonomous) nor a failure. A turn that was overridden is counted
    # once under its own status too (ticket 01a1134b-2cb8): it ran, but the row says something else ended the session, so
    # it is not a completion a dashboard should add up.
    _observe_turn(
        session,
        "overridden" if overridden
        else "failed" if ended_reason == "failed"
        else "tool_turn_cap" if last_done_reason == "tool_turn_cap"
        else "completed",
        _turn_started_at,
    )
    if not overridden:
        # No reply is announced for a session the row says was ended under the turn: the durable event log must not say
        # both (``session.ended`` below carries the row's own reason).
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
        deps, session, written.status, written.ended_reason,
    )
    if ended_reason == "failed" and not overridden:
        # A turn the clean arm ends ``failed`` (``Done(stop_reason="error")``, a failed graph run) is a failed turn too (C-024): announced like the
        # exception exit's, with the stop reason as its code. It stamps no ``last_turn_error`` (only the exception exit does).
        await _event_recorder(deps).emit(
            "session.turn_failed",
            workspace_id=session.workspace_id,
            session_id=session_id,
            payload={"code": last_done_reason or "turn_failed", "ended": True},
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
    # 01a0518a: a clean stop/end_turn/stop_sequence now rests the session
    # parked (WAITING/None) instead of ending it - the same "a final
    # answer was produced" moment the ENDED+completed branch below used
    # to catch, just without the session terminating to get there.
    # Excludes an executor-set WAITING (assistant-asked-a-question
    # heuristic): that already resolved to WAITING+None BEFORE the flip
    # too and never relayed for an unmapped session, so it stays excluded
    # here to avoid changing that pre-existing behavior.
    clean_stop_now_parked = (
        new_status == SessionStatus.WAITING
        and ended_reason is None
        and agent_status != SessionStatus.WAITING
        and last_done_reason in ("stop", "end_turn", "stop_sequence")
    )
    # A tool-turn-cap trip relays in ALL three shapes (thread-mapped, autonomous ENDED/tool_turn_cap, plain interactive
    # WAITING), as a stopped-short message and not as a reply: none of the arms above covered it (an autonomous capped
    # run posted nothing at all; a thread-mapped one posted its partial text as if it were the answer).
    capped_now = last_done_reason == "tool_turn_cap"
    if deps.channel_dispatcher is not None and not overridden and (
        relay_every_turn
        or (
            new_status == SessionStatus.ENDED
            and ended_reason == "completed"
        )
        or clean_stop_now_parked
        or capped_now
    ):
        try:
            from primer.channel.reply_binding import resolve_reply_binding
            from primer.channel.session_relay import (
                post_session_final_result,
                read_session_final_text,
                stopped_short_message,
            )

            # The binding FIRST, and nothing is read without one: this runs after every clean turn of every
            # session, and the read is the whole messages.jsonl over the workspace's runtime connection (a
            # docker or k8s workspace pulls it across a websocket). A session with no channel, or a quiet
            # binding, has nothing to post and costs no workspace I/O here (one Workspace-row lookup, for
            # the workspace-standing binding).
            binding = await resolve_reply_binding(session, storage_provider=deps.storage_provider)
            if binding is not None and not getattr(binding, "quiet", False):
                final_text: str | None = None
                read_timed_out = False
                try:
                    # Bounded: a read that never returns (the runtime connection is down and its client waits
                    # for it forever) must not hold the lease release that comes after this.
                    async with asyncio.timeout(_BEST_EFFORT_IO_TIMEOUT_S):
                        final_text = await read_session_final_text(
                            await _final_text_source(deps, session), session_id,
                        )
                except TimeoutError:
                    read_timed_out = True
                    logger.warning(
                        "session %s: reading the final text for the channel relay did not finish within %gs "
                        "(the workspace is not answering); %s",
                        session_id, _BEST_EFFORT_IO_TIMEOUT_S,
                        "the stopped-short notice is posted without it" if capped_now
                        else "nothing was posted to the channel",
                    )
                if capped_now:
                    # The notice does not depend on the read: a run that stopped at its tool-turn cap is told to the
                    # channel even when its partial text could not be read, and the partial text is never posted bare.
                    final_text = stopped_short_message(final_text)
                if final_text:
                    try:
                        # Bounded like the read, for the same reason: the lease release comes after this, and
                        # a channel API call that never returns (a hung Discord or Slack request) would hold it.
                        async with asyncio.timeout(_CHANNEL_POST_TIMEOUT_S):
                            await post_session_final_result(
                                dispatcher=deps.channel_dispatcher,
                                session=session,
                                storage_provider=deps.storage_provider,
                                text=final_text,
                                binding=binding,
                            )
                    except TimeoutError:
                        logger.warning(
                            "session %s: posting the final result to the channel did not finish within %gs "
                            "(the channel API is not answering); it may or may not have been posted",
                            session_id, _CHANNEL_POST_TIMEOUT_S,
                        )
                elif not read_timed_out:
                    # A reply-bound session that relays nothing was invisible for months (the reader had no
                    # read surface and returned None, and ``if final_text`` skipped the post). Say so: the
                    # final text of a turn that reached this point is normally there.
                    logger.warning(
                        "session %s: reply-bound, but no final text could be derived from its messages.jsonl; "
                        "nothing was posted to the channel", session_id,
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


async def _final_text_source(deps: SessionDispatchDeps, session: WorkspaceSession) -> Any:
    """What ``read_session_final_text`` reads ``messages.jsonl`` through: the session's REAL workspace.

    ``deps.workspace_io`` is the pool's ``_WorkspaceIOShim``, a WRITE adapter (``append_message_line``): it has neither
    ``read_lines`` nor ``read_file``, so the reader found nothing to read and returned None. The workspace the registry
    resolves does (``read_file`` over its ``state_path``), the same object the trigger hold reads the final text
    through. ``deps.workspace_io`` stays the fallback for a deployment without a registry (and for the test fakes that
    expose ``read_lines``)."""
    registry = deps.workspace_registry
    if registry is not None:
        try:
            workspace = await registry.get_workspace(session.workspace_id)
        except Exception:  # noqa: BLE001 - the relay degrades, it never blocks the release
            logger.warning(
                "session %s: workspace %s could not be resolved for the final-result relay",
                session.id, session.workspace_id, exc_info=True,
            )
            workspace = None
        if workspace is not None:
            return workspace
    return deps.workspace_io


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
#   primer/toolset/misc.py:213        "timer:<sid>:<tcid>" (a graph node adds ":<node>" before the tcid)
#   primer/toolset/workspaces.py:511  "watch:<sid>:<tcid>"
#   primer/toolset/mcp.py:223         "mcp_task:<tsid>:<task_id>"
#   primer/toolset/trigger.py:908     "trigger:<sid>:<tid>"
# Order matches the most-specific prefix-first principle so "tool_approval:"
# doesn't accidentally match an earlier shorter prefix.
# The table itself lives with ``Yielded`` (primer.model.yield_.YIELD_KIND_PREFIXES): the agent loop reads the same
# one to tell a human gate (still parks under a Stop) from a park that waits on no decision (ended by it).
_YIELD_KIND_PREFIXES = YIELD_KIND_PREFIXES


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


def _make_scoped_call_resolver(
    coalesce_state: _CoalesceState,
) -> "Callable[[str], tuple[str, int]]":
    """Build the per-turn resolver the claim-based dispatch seam uses to
    turn a raw provider tool-call id into ``(scoped_id, record_seq)``.

    Two-step lookup against THIS turn's ``coalesce_state``:
    ``scoped_call_ids`` (raw id -> scoped id, minted at ``ToolCallStart``)
    then ``tool_call_record_seq`` (scoped id -> the durable TOOL_CALL
    record's own seq, populated by the append loop above right after the
    record is flushed). A miss on either means the ordering invariant the
    whole design depends on - the TOOL_CALL record is durable BEFORE its
    scoped id is ever resolvable through this callable - broke somewhere
    upstream. Raise loudly rather than silently degrading into a
    malformed ``ToolCallTask``; a successful return is itself the "this
    call's record is durable" proof the ``except ToolWaitPark`` handler
    relies on (see ``_dispatch_as_claims``'s own docstring).
    """
    def _resolve(raw_call_id: str) -> tuple[str, int]:
        scoped_id = coalesce_state.scoped_call_ids.get((None, raw_call_id))
        if scoped_id is None:
            raise RuntimeError(
                f"resolve_scoped_call: no scoped id for raw tool-call id "
                f"{raw_call_id!r} - ToolCallStart never minted one this turn"
            )
        record_seq = coalesce_state.tool_call_record_seq.get(scoped_id)
        if record_seq is None:
            raise RuntimeError(
                f"resolve_scoped_call: scoped id {scoped_id!r} has no "
                "durable TOOL_CALL record_seq yet - the durable-append-"
                "before-claimable invariant broke"
            )
        return scoped_id, record_seq
    return _resolve


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


def _tool_wait_yielded_record(tool_wait: ToolWaitPark) -> SessionMessageRecord:
    """Build a YIELDED SessionMessageRecord for a tool_wait batch park.

    Batch-shaped counterpart to ``_yielded_record`` above: there is no
    single ``tool_name``/``tool_call_id`` to report (the park spans N
    independently-claimable tasks), so the payload carries the task id
    lists instead.
    """
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.YIELDED,
        payload={
            "event_key": tool_wait.event_key,
            "kind": "tool_wait",
            "outstanding_task_ids": list(tool_wait.outstanding_task_ids),
            "notifying_task_ids": [
                scoped_id for scoped_id, _ in tool_wait.notifying_results
            ],
        },
        created_at=_now(),
    )


# The ``reason`` of a CANCELLED record / TurnLogCancelled. A Stop (the session stays alive) and a Cancel (it
# ends) are told apart by this alone, e.g. by the console's lifecycle label.
_STOP_REASON = "operator_interrupt"
_CANCEL_REASON = "operator_cancel"

# How long the CANCELLED record may take to reach the workspace while the session's lifecycle lock is held.
# A workspace whose runtime connection dropped (a common reason to press Stop) blocks the write until it
# reconnects, which may be never, and every Cancel, Stop, steer, pause, resume and switch of the session
# queues behind that lock. Long enough for a slow healthy write, short enough that the session is not wedged.
# (The shared in-lock deadline, ``mutation_lock.IN_LOCK_IO_TIMEOUT_S``; this name is kept for the cancelled exit.)
_CANCELLED_RECORD_WRITE_TIMEOUT_S = IN_LOCK_IO_TIMEOUT_S

# The same bound for the ENDED transition's mirror onto the executor's on-disk slot (``session.json``), which
# commits through the same runtime connection and runs inside the same lock on a Cancel.
_SLOT_MIRROR_TIMEOUT_S = 10.0

# The bound for the cancelled exit's best-effort workspace I/O OUTSIDE the lock (the output streamed before the
# Stop, the turn-log entry, the turn log's close). Nothing else waits on those, but the terminal publish and the
# lease release come after them. Shorter than the others so the whole exit adds up to _TERMINAL_EXIT_GRACE_S:
# _CANCELLED_RECORD_WRITE_TIMEOUT_S + _SLOT_MIRROR_TIMEOUT_S under the lock, then two of these (10 + 10 + 5 + 5 = 30)
# with NO margin, so a step that is merely slow lets the grace cancel the tail; what that skips is only what comes
# after the terminal publish. That tail now includes the drain checkpoint (``_apply_pending_switch_at_checkpoint``, then the
# steer realize), whose switch takes the lifecycle lock itself and has its own IN_LOCK_IO_TIMEOUT_S on top of the 30 above, plus the
# wait for another in-lock writer to let go: past the grace it is cancelled, and a switch it did not finish stays queued for the
# next checkpoint. (The build-failure path is not this exit, but it can hold the lock for two
# _SLOT_MIRROR_TIMEOUT_S bounds: loading the slot through the registry, then the mirror.)
_BEST_EFFORT_IO_TIMEOUT_S = 5.0

# The bound for the final-result post to the session's channel, which also runs before the lease release (see
# post_session_final_result's call site): a channel API call that never returns would hold the release. Longer than
# _BEST_EFFORT_IO_TIMEOUT_S because it is a real network call (a get-or-create thread, then the post, with the
# platform's own rate-limit waits) and not a workspace read. A timeout is logged; the message may or may not have
# reached the channel.
_CHANNEL_POST_TIMEOUT_S = 15.0


# How long a cancelled turn's exit may keep running after the TASK is cancelled under it (see _finish_despite_cancel).
_TERMINAL_EXIT_GRACE_S = 30.0


async def _finish_despite_cancel(exit_coro: "Awaitable[ReleaseOutcome]") -> ReleaseOutcome:
    """Run a turn's terminal exit to completion even if the task awaiting it is cancelled meanwhile.

    The worker pool delivers a Cancel two ways: the cooperative signal this turn watches, and a HARD
    preempt that cancels the whole task wherever it is. The Cancel route KEEPS the lease of a RUNNING
    session (it flags the row, publishes the key and sends the NOTIFY), so the hard path of a Cancel is
    ``_cancel_loop``'s and the row reconciler's ``cancel_once``. The heartbeat's lost-lease verdict
    (``scope.cancel("preempted")``) comes from a force-delete, which drops the lease, or from a lease that
    was really stolen or expired; and the drain timeout cancels unconditionally. By the time a turn is in
    its cancelled exit it has already decided to end, and the exit IS the convergence the hard preempt
    asks for. Cut mid-way, the
    pool's own convergence finds a row that is already ENDED and skips it, so the terminal event (the
    webhook hold waits on it), the turn log, the queued-steer drain and the metric were silently lost.

    So the exit runs as its own task, and a cancellation that arrives while it runs is absorbed until
    the exit is done. What the caller then sees depends on how the exit ended:

    * It finished: the cancellations are consumed (``uncancel``, down to the task's ``cancelling()`` count
      on entry) and its own outcome is returned, exactly as for a cancel that no one preempted. Re-raising here would throw
      the outcome away: the pool would release with its pre-set ``success=False`` (its convergence skips a
      row that is already ENDED) and ``on_release`` would write a terminal ERROR after a clean exit.
    * It raised: the caller gets a ``CancelledError`` chained to the exit's error (which is logged), not the
      error itself. The pool's preempt convergence runs only on a ``CancelledError``; the error would skip it
      and leave the session RUNNING with no lease.

    The shelter is BOUNDED by ``_TERMINAL_EXIT_GRACE_S`` from the first cancellation: past that the exit
    is abandoned (cancelled) and the cancellation propagates, so a drain timeout can still abort an exit
    that hangs on a dead storage or workspace. An abandoned exit is not awaited, so a done-callback
    retrieves whatever it dies of and logs it.

    Two limits follow from the shelter. A drain (or a lost-lease verdict) can take up to the grace longer
    to take effect on a turn that is in its exit, so a drain can run that much past its budget. And an exit
    abandoned at the grace that LATER finishes cleanly (it swallowed its cancel, or was past its last await)
    was already released by the pool as a failure: its effects landed, but the claim was not released as a success.
    """
    task = asyncio.ensure_future(exit_coro)
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    entered_cancelling = current.cancelling() if current is not None else 0
    absorbed = 0
    deadline: float | None = None
    while not task.done():
        timeout = None if deadline is None else max(0.0, deadline - loop.time())
        try:
            # asyncio.wait does NOT cancel the task it waits on when the awaiting task is cancelled.
            await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            absorbed += 1
            if deadline is None:
                deadline = loop.time() + _TERMINAL_EXIT_GRACE_S
                logger.info(
                    "dispatch: the task was cancelled while the cancelled turn's exit was running; "
                    "finishing the exit first (for at most %gs)", _TERMINAL_EXIT_GRACE_S,
                )
            continue
        if not task.done():
            task.add_done_callback(_consume_abandoned_exit)
            task.cancel()           # the grace is up: stop sheltering an exit that is not finishing
            raise asyncio.CancelledError()
    if not absorbed:
        return task.result()
    if task.cancelled():
        raise asyncio.CancelledError()
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "dispatch: the cancelled turn's exit failed after the task was cancelled; "
            "propagating the cancellation", exc_info=exc,
        )
        raise asyncio.CancelledError() from exc
    if current is not None:
        # ``cancelling()`` counts cancel() REQUESTS, not the CancelledErrors delivered: two requests before the
        # task runs again deliver one. Consume down to what it was on entry, not once per delivery.
        while current.cancelling() > entered_cancelling:
            current.uncancel()
    return task.result()


def _consume_abandoned_exit(task: "asyncio.Task") -> None:
    """Done-callback of an exit that ``_finish_despite_cancel`` stopped waiting for: retrieve what it ended
    with (asyncio reports 'Task exception was never retrieved' otherwise) and log an error."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("dispatch: an abandoned cancelled-turn exit ended with an error", exc_info=exc)
        return
    logger.info(
        "dispatch: an abandoned cancelled-turn exit later finished successfully; the pool had already "
        "released the lease as a failed one, which is why a terminal ERROR record can follow its CANCELLED record"
    )


async def _land_cancelled_turn(
    deps: "SessionDispatchDeps",
    session: "WorkspaceSession",
    *,
    executor: Any,
    writer: "WorkspaceMessageWriter",
    turn_log: Any,
    started_at: datetime,
) -> ReleaseOutcome:
    """The ONE exit of a cancelled turn: a Stop (the session stays alive) or a Cancel (it ends).

    Both the cancel arm and the arm that lands a Cancel found after the stream's last event come here, so
    they cannot drift apart again (the second used to be a half-copy of the first).

    Stop-versus-Cancel is decided from the row INSIDE the lifecycle lock, and the CANCELLED record is written
    there too, after the decision and before the status transition. The Cancel route takes the same lock, so
    a Cancel that lands between the turn noticing the signal and this lock is seen (a decision taken before
    the lock would land WAITING on a row whose ``cancel_requested`` is already set). That lock is
    process-local (see ``primer.session.mutation_lock``): it serializes a worker and an API handler that share
    a process, and not one on another host, where only the committed flag this read sees is shared.
    The late-cancel arm drops the lock after finding the Cancel and takes it again here, so the row can
    change in between: this re-reads and decides again, and a row that is already ENDED, or gone (a
    force-delete), is left alone (no record, no overwrite; the slot is mirrored with the row's own reason when it
    accepts one, as in the terminal write's skip) while the terminal event and the release still happen. Not every cancelled turn comes through here: a turn whose Cancel was already on
    the row when it was claimed takes the short-circuit at the top of ``run_one_session_turn`` (ENDED/
    cancelled without running). A Cancel ALWAYS sets
    ``cancel_requested`` before it publishes, so that flag alone separates the two: not set means a Stop.
    ``interrupt_requested`` is deliberately NOT read: a human steer that lands mid-turn flips ``turn_status``
    to claimable and ``wake_session`` then clears it, which turned a Stop into a hard End for exactly the
    user who pressed Stop and then typed. The record is written before the transition so a reader that sees
    the new status finds the record that explains it, and its ``reason`` follows the decision (the console
    labels the first "stopped" and the second "cancelled").
    """
    session_id = session.id
    session_storage = deps.storage_provider.get_storage(WorkspaceSession)
    # Serialize the terminal transition + interrupt-flag clear against a concurrent resume/pause/cancel/
    # interrupt API call (T0432-style lost update; see primer.session.mutation_lock). The flag is cleared on
    # BOTH branches so one that lost the decision (a Cancel won) cannot persist into a future turn either.
    async with session_lifecycle_lock().acquire(session_id):
        fresh = await session_storage.get(session_id)
        if fresh is None or fresh.status == SessionStatus.ENDED:
            # The row's fate was decided while this turn ran: a force-delete flagged it, wrote
            # ENDED/force_deleted and is removing the row and its on-disk slot, or the row is already gone.
            # Leave it alone: no CANCELLED record (it would recreate a transcript in the workspace of a
            # deleted session), no overwrite of the reason (ENDED/force_deleted would become
            # ENDED/cancelled). The slot follows the row's own reason, as in the terminal write's skip (the
            # pool's _end_session ends a row without touching it, so nothing else would take session.json
            # out of RUNNING); a force-deleted row gets no mirror (the delete is removing the slot, and the
            # reason is not one the slot accepts), nor does a row that is gone. The rest of the exit still
            # runs so the terminal event (the webhook hold waits on it) and the lease release are not lost.
            logger.info(
                "session %s: the row is %s; the cancelled exit leaves it as it is",
                session_id, "gone" if fresh is None else f"already ENDED ({fresh.ended_reason})",
            )
            reason = _CANCEL_REASON
            seq = None
            new_status = SessionStatus.ENDED
            if fresh is None:
                ended_reason = "force_deleted"
            else:
                ended_reason = fresh.ended_reason
                await _mirror_ended_row_onto_slot(session, fresh, executor=executor, workspace_registry=None)
        else:
            is_interrupt = not fresh.cancel_requested
            reason = _STOP_REASON if is_interrupt else _CANCEL_REASON
            seq = await _write_cancelled_record(writer, session_id, reason)
            if is_interrupt:
                new_status, ended_reason = _interrupt_post_status()
            else:
                new_status, ended_reason = SessionStatus.ENDED, "cancelled"
            written = await _transition_session_status(
                session_storage,
                session,
                new_status=new_status,
                ended_reason=ended_reason,
                executor=executor,
                expected_epoch=session.binding_epoch,
            )
            own_outcome = not _ended_by_another_path(written, new_status, ended_reason)
            # The row may have been ended between the decision above and the write by a writer that does
            # not take this lock: announce what it says.
            new_status, ended_reason = written.status, written.ended_reason
            await _clear_interrupt_requested(session_storage, session_id)
            await _persist_last_seq(session_storage, session_id, writer.last_seq)
            await _advance_drain_cursor(session_storage, session_id)
            # Last write of the block: present means the cursor already advanced. Not for a row another path
            # ended meanwhile (the turn's own write was skipped), as in the early branch above.
            if own_outcome:
                await _mark_turn_completed(session_storage, session_id, session.turn_no)
    if seq is not None:
        await deps.event_bus.publish(f"session:{session_id}:tick", {"seq": seq})
    await _best_effort_io("the TurnLogCancelled turn log entry", session_id, _safe_turn_log(turn_log, TurnLogCancelled(
        seq=0, ts=_now(), turn_no=session.turn_no, reason=reason,
    )))
    await _cancel_the_stopped_external_call(deps, session, executor)
    await _publish_terminal(deps, session, new_status, ended_reason)
    await _best_effort_io("closing the turn log", session_id, turn_log.aclose())
    await _apply_pending_switch_at_checkpoint(deps, session)
    await _realize_pending_at_checkpoint(deps, session)
    _observe_turn(session, "cancelled", started_at)
    return ReleaseOutcome(success=True, drop_lease=True)


async def _cancel_the_stopped_external_call(
    deps: "SessionDispatchDeps", session: "WorkspaceSession", executor: Any,
) -> None:
    """Cancel the pending ``ExternalToolCall`` row of an ``external_tool`` call that a Stop ended instead of parking.

    That is a park the Stop ended (``executor.stopped_park``), or a call of an invoker-supplied tool that the Stop
    cancelled or abandoned before it reached its yield (``executor.stopped_calls``, stop slice B1).

    The invoker-supplied tool provider writes the row BEFORE it yields (``primer/agent/external_tools.py``), and a turn
    that ends instead of parking would leave it listed as pending (``GET .../external_tools/pending``, the global list)
    and answerable only with a 409 (an answer needs a parked row). The cancel, delete, restart and steer routes cancel
    such rows with the same call. Row-side only: there is no park to wake. It runs just BEFORE the terminal publish
    (outside the lifecycle lock), so a listener that waits for the terminal event never sees a pending row for a turn
    that has ended; it is bounded by ``_BEST_EFFORT_IO_TIMEOUT_S``, so a slow storage delays the publish by at most
    that, and it never fails the exit.
    """
    from primer.agent.external_tools import external_event_key, is_external_tool_name

    park = getattr(executor, "stopped_park", None)
    ended_an_external_park = isinstance(park, YieldToWorker) and (park.yielded.event_key or "").startswith(
        external_event_key(session.id, ""),
    )
    # A call the Stop CANCELLED (or abandoned) never reached its yield, but its provider may already have written the
    # row (stop slice B1: cancelling is now the default for a call that is running when the Stop lands).
    cancelled_an_external_call = any(
        is_external_tool_name(getattr(call, "name", "") or "") for call in (getattr(executor, "stopped_calls", None) or [])
    )
    if not (ended_an_external_park or cancelled_an_external_call):
        return
    from primer.model.external_tool import ExternalToolCall
    from primer.session.external_tools import cancel_pending_external

    try:
        await _best_effort_io(
            "cancelling the stopped external tool call", session.id,
            cancel_pending_external(
                call_storage=deps.storage_provider.get_storage(ExternalToolCall),
                session_id=session.id, reason="stopped by user",
            ),
        )
    except Exception:  # noqa: BLE001 - the cancelled exit must still finish
        logger.warning(
            "session %s: could not cancel the pending external tool call a Stop ended", session.id, exc_info=True,
        )


async def _best_effort_io(what: str, session_id: str, work: "Awaitable[Any]") -> None:
    """Await best-effort workspace I/O of a cancelled exit that runs OUTSIDE the lifecycle lock, bounded.

    A write that never returns (the workspace's runtime connection dropped) would hold the terminal publish and
    the lease release that come after it. On a timeout this logs and carries on without it; any other failure
    of ``work`` is the caller's, exactly as before.
    """
    try:
        async with asyncio.timeout(_BEST_EFFORT_IO_TIMEOUT_S):
            await work
    except TimeoutError:
        logger.warning(
            "session %s: %s was not confirmed within %gs (the workspace is not accepting writes); "
            "carrying on without it", session_id, what, _BEST_EFFORT_IO_TIMEOUT_S,
        )


async def _row_holds_a_cancel(session_storage: Any, session_id: str) -> bool:
    """True when the row says a user Cancel is pending: ``cancel_requested`` set and the row not ended.

    The same test as the pool's preempt convergence. A row that is already ENDED (a force-delete wrote ENDED/``force_deleted`` and
    is removing it) or gone is not a Cancel to land: ``_land_cancelled_turn`` would leave it alone, and the pool's convergence
    skips it."""
    row = await session_storage.get(session_id)
    return row is not None and bool(row.cancel_requested) and row.status != SessionStatus.ENDED


async def _flush_and_tick(
    deps: "SessionDispatchDeps", writer: "WorkspaceMessageWriter", session_id: str,
    record: "SessionMessageRecord | None" = None,
) -> int:
    """Make everything the writer holds durable, THEN tell the tap how far the log now goes. Returns that seq.

    ``record`` is appended first, UNDER THE SAME CATCH as the flush: a park arm's own record (YIELDED, tool_wait) is appended inside
    an ``except YieldToWorker`` / ``except ToolWaitPark`` handler, where nothing catches a sibling error, and that append runs the
    writer's age flush (the tool_call record buffered when the tool started is older than 100 ms by the time the park lands), so it
    is the call that meets a dead batch. A park whose record is lost still parks: the row's park columns are what wake it.

    The tap reads the durable log when a ``session:{sid}:tick`` wakes it and has no reason to look again before the next
    one. The per-record tick right after ``append`` goes out while the record is still in the writer's buffer (100 ms or
    16 KB, and the age is only checked on the next append), so for the last records of a turn that tick announces nothing the
    tap can read. The tick that matters is the one after the flush, and it names ``writer.last_seq``: the flush took every
    buffered record, not just the last one appended. Every exit that flushes a turn's tail goes through here (the clean
    completion, a failed stream, both parks), so a record is never made durable without a tick that follows it; the
    cancelled exit flushes in ``_write_cancelled_record`` and publishes its tick after the lifecycle lock is released.

    The flush is bounded by the writer (``WorkspaceMessageWriter`` abandons a batch the workspace has not answered within
    ``_WRITE_TIMEOUT_S`` and breaks): a workspace that never answers used to hold every one of these exits, and with them the lease
    release and the session's lifecycle, for ever. The records the workspace did not take are lost (the writer logged which seqs); the
    exit still lands and the tick still goes out, naming what the writer numbered, since the tap only reads the log up to where it
    really goes. A write that FAILS (not a timeout) still raises, as before.
    """
    try:
        if record is not None:
            await writer.append(record)
        await writer.flush()
    except WorkspaceWriteTimeout:
        logger.warning(
            "session %s: the turn's last records were not made durable (the workspace is not accepting writes); the exit carries on",
            session_id,
        )
    seq = writer.last_seq
    await deps.event_bus.publish(f"session:{session_id}:tick", {"seq": seq})
    return seq


async def _write_cancelled_record(
    writer: "WorkspaceMessageWriter", session_id: str, reason: str,
) -> int | None:
    """Write the CANCELLED record, bounded, for a caller that holds the session's lifecycle lock.

    Returns the record's seq, or ``None`` when the workspace did not take the write within
    ``_CANCELLED_RECORD_WRITE_TIMEOUT_S``. A record that is lost is logged and the exit carries on: the row's
    status and ``ended_reason`` still say how the turn ended, and the alternative is a lock that every other
    operation on the session queues behind for as long as the workspace stays unreachable. Any other failure
    of the write is not swallowed. A cancellation of the caller is not a timeout and passes through.
    """
    try:
        async with asyncio.timeout(_CANCELLED_RECORD_WRITE_TIMEOUT_S):
            seq = await writer.append(_cancelled_record(reason))
            await writer.flush()
            return seq
    except TimeoutError:
        logger.warning(
            "session %s: the CANCELLED(%s) record was not confirmed within %gs (the workspace is not "
            "accepting writes); finishing the cancelled exit without it",
            session_id, reason, _CANCELLED_RECORD_WRITE_TIMEOUT_S,
        )
        return None


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

# PHASE 1 item 3 of the execution-lifecycle revamp (01a04d91-a7a0) -
# USER-CONFIRMED (01a0518a): a clean stop/end_turn rests the session
# PARKED (resumable) rather than ENDED+reopen-gate, matching how a
# yielding-tool park already behaves. Default is now True; the module-
# level flag stays in place as a test seam (_post_turn_status checks it
# at call time, not baked into the frozen _STOP_REASON_TO_STATUS dict
# above, so a test can still flip it per-case).
#
# The three edges the seam's own documentation called out before the
# flip, resolved as part of 01a0518a:
#   1. wake_session's reopen path (primer.session.enqueue) only special-
#      cases an ENDED row ("Sending a NEW message to an ENDED session
#      reopens it"). Audited: NO change needed. A session resting at
#      WAITING never had its slot closed, so none of the ENDED-reopen
#      steps (slot.reopen(), INVOCATION_DIVIDER, invocation-counter
#      bump) apply - wake_session's existing _RESUMABLE set already
#      includes WAITING (and PAUSED), so `row.status in _RESUMABLE ->
#      RUNNING` on the normal (non-ENDED) branch already promotes a
#      resting session correctly with zero code changes.
#   2. session_state (WorkspaceSession.session_state) now distinguishes
#      "genuinely idle, never done anything" from "resting after a
#      completed turn" via turn_no > 0 (see that property's docstring
#      for the full justification) - the former stays "waiting", the
#      latter now reads "parked".
#   3. ENDED consumers (lists/filters/counts/sweepers/analytics) were
#      swept for accumulation effects now that a clean stop no longer
#      reaches ENDED. Old ENDED rows are unaffected and stay ENDED.
_CLEAN_TURN_RESTS_PARKED = True


def _post_turn_status(
    last_done_reason: str | None,
    agent_status: SessionStatus | None,
    *,
    autonomous: bool = False,
) -> tuple[SessionStatus, str | None]:
    """Decide the WorkspaceSession.status to write after a clean turn.

    Precedence: a definitive AgentSession decision wins (the executor
    set ENDED on internal error, WAITING on a user-input prompt heuristic,
    etc.). Otherwise fall back to the LLM's last stop reason. The default
    when neither is informative is ENDED/completed.

    With ``_CLEAN_TURN_RESTS_PARKED`` now on by default (01a0518a), a
    clean agent turn rests the session WAITING (served as
    session_state="parked" - see ``WorkspaceSession.session_state``)
    instead of ENDING it. Sending a NEW message to a resting session
    resumes it in place (``wake_session``'s existing ``_RESUMABLE`` set
    already includes WAITING); a genuinely ENDED session still reopens
    via ``wake_session``'s ENDED branch. The executor-set WAITING
    (assistant-asked-a-question heuristic) is a distinct, legitimate
    wait and is preserved below - both read "parked" once turn_no > 0,
    since the served vocabulary doesn't distinguish the two reasons.

    ``autonomous`` (``primer.session.autonomy.session_is_autonomous`` -
    studio-agents-interact §8.1, pre-existing) is the one exemption to
    the parked rest: a self-driving session (a graph, or an agent with
    ``autonomous=True`` - trigger/webhook-fired one-shot sessions set
    this explicitly, see ``agent_fresh_session.py``) has no interactive
    human to resume it, so it must still END on a clean turn, exactly as
    before the flip. Without this exemption a one-shot trigger session
    rests parked forever, and a ``parallelism="skip"`` subscription gate
    keyed on "any non-ENDED session with turn_no > 0" (see
    ``primer.trigger.subscribers.session_holds_skip_gate``) would wedge
    permanently closed after its very first successful fire.

    ``_CLEAN_TURN_RESTS_PARKED`` (see its own module-level docstring,
    right above ``_STOP_REASON_TO_STATUS``) is checked FIRST, before the
    executor-set-ENDED precedence below, so a definitive internal error
    still always ENDs the session even with the flag on - only the plain
    clean-stop case changes. Flipping the flag off (test seam) restores
    the old behavior: every clean turn ENDS the session.
    """
    if (
        _CLEAN_TURN_RESTS_PARKED
        and not autonomous
        and last_done_reason in ("stop", "end_turn", "stop_sequence")
        and agent_status != SessionStatus.ENDED
    ):
        return (SessionStatus.WAITING, None)
    # An executor-set ENDED is authoritative.
    if agent_status == SessionStatus.ENDED:
        # Translate ended-but-stop-reason into a finer reason when we can.
        mapped = _STOP_REASON_TO_STATUS.get(last_done_reason or "", (None, None))
        return (SessionStatus.ENDED, mapped[1] or "completed")
    # Executor-set WAITING (e.g. assistant asked a question heuristic).
    if agent_status == SessionStatus.WAITING:
        return (SessionStatus.WAITING, None)
    # The model kept asking for tools and max_tool_turns stopped the turn. Its last model
    # event was Done(tool_use), which the mapping below reads as "the executor will queue
    # the next turn itself" (RUNNING); nothing does after a cap trip, and boot recovery
    # re-arms every RUNNING row, so it must NOT map there. An interactive session rests
    # WAITING (the user can send another message); an autonomous one has nobody to resume
    # it and ENDS with its own reason, as it does after a clean turn.
    if last_done_reason == "tool_turn_cap":
        if autonomous:
            return (SessionStatus.ENDED, "tool_turn_cap")
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
    seq=1. Only ADVANCES ``last_seq`` (never downgrades) so a concurrent steer
    that already wrote a higher USER_INPUT seq is not clobbered: ONE
    field-scoped ``patch_if`` fenced on the value read
    (:func:`primer.session.seq_reservation.advance_last_seq`), not a ``get`` +
    whole-document ``update``, which would write a row that moved in between
    back over. Called from inside a ``session_lifecycle_lock`` critical
    section, the same lock ``wake_session``/``reset_session`` use for their own
    ``last_seq`` writes; the fence is what protects it from a writer that does
    not take that lock.
    """
    from primer.session.seq_reservation import advance_last_seq

    await advance_last_seq(session_storage, session_id, seq)


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


def _ended_by_another_path(written: "_TerminalWrite", status: SessionStatus, ended_reason: str | None) -> bool:
    """The turn's terminal write was skipped because something else ended the row first (a force-delete, the pool's
    preempt convergence, the reconciler), so the row does not carry this turn's outcome.

    Not the same as ``not written.landed``: an identical repeat (the row already had the outcome asked for) and an
    epoch-voided write also do not land, yet the turn's own outcome stands there.
    """
    return not written.landed and (written.status, written.ended_reason) != (status, ended_reason)


async def _mark_turn_completed(session_storage, session_id: str, turn_no: int) -> None:
    """Record that turn ``turn_no`` has committed every effect it has (``WorkspaceSession.completed_turn_no``).

    Called ONLY as the last write of the two terminal lock blocks of a turn that ran (the clean completion and
    the Stop/Cancel exit), and there only when the turn's own outcome stands (not when another path ended the row
    first: ``_ended_by_another_path``, and the cancelled exit's early branch). Never from ``_end_turn_failed`` (a
    failed release does not bump ``turn_no``, so a reopened session would match its own marker), a park or an early
    exit. It does not depend on the cursor moving, and is deliberately not part of ``_advance_drain_cursor``, which
    the failed exit shares and which writes only when the cursor moves.

    One field-scoped ``patch_if`` fenced on the turn's own ``turn_no``: it writes nothing else, and it writes
    nothing if the row has moved on to another turn. Best-effort: a rejected fence or a storage error is logged
    and never fails the turn (without the marker a rolled-back release re-runs the turn, as it did before).
    """
    try:
        written = await session_storage.patch_if(
            session_id, {"completed_turn_no": turn_no}, where={"turn_no": [turn_no]},
        )
    except Exception:  # noqa: BLE001 - best-effort; the turn's own outcome stands
        logger.warning(
            "session %s: could not record turn %d as completed; a rolled-back release would run it again",
            session_id, turn_no, exc_info=True,
        )
        return
    if written is None:
        logger.warning(
            "session %s: turn_no is no longer %d, so the turn is not recorded as completed",
            session_id, turn_no,
        )


async def _noop_if_turn_already_completed(
    deps: SessionDispatchDeps, session_storage, session_id: str,
) -> ReleaseOutcome | None:
    """The no-op path for a claim of a turn that already completed (01a10b05); ``None`` means run the turn.

    A completed turn commits everything BEFORE its release (records, status, ``last_seq``, the cursor, then
    ``completed_turn_no``); only ``turn_no + 1`` is written inside the release transaction. A release that is
    abandoned at the pool's bound, or raises, rolls that back and leaves the lease claimed until it expires, and
    the re-claim used to run a NEW turn over the same history (a second model call and its tool runs). A fresh
    row with ``completed_turn_no == turn_no`` is exactly that state, so this claim does not run the turn: it
    returns ``success=True`` and its own release applies the lost bump (once; the adapter fences it), after which
    the marker trails ``turn_no`` and every later claim runs normally. If that release is lost too, the next
    claim lands here again. ``turn_no`` is never bumped here.

    What the claim leaves armed depends on whether input is unanswered, decided in this order under the lock:

    * ``turn_status == "claimable"``: a steer is queued; the pool re-arms it after the release.
    * otherwise the log is read and ``has_open_turn`` (``primer/session/turns.py``: a USER_INPUT at or after the
      drain cursor with no closing record) decides. An open input is armed with a ``turn_status``-only patch
      (``claimable``, fenced on a RUNNING or WAITING row), so the pool re-arms it and the next claim answers it. This catches a stale whole-document
      write that put back an old ``turn_no``, ``turn_status`` and ``last_seq`` over a steer. ``next_unprocessed_seq
      <= last_seq`` is NOT the test: compaction, rewind, reopen and abandon raise ``last_seq`` with a marker record
      and leave the cursor, and a double stale revert can leave the cursor past ``last_seq``.
    * no open input: the drain checkpoint the turn would have run is run here (a queued binding switch, then ONE
      queued steer, which arms itself through ``wake_session``), recovering a crash between the marker and the
      checkpoint. With nothing queued there is nothing to arm.

    The row is judged by its status, on every read the guard makes under the lock. A row another process PAUSED or
    ENDED after this turn's own top read (the ENDED, cancel and pause exits of ``run_one_session_turn`` decided from
    that read, and nothing re-checks the status afterwards) is settled, whatever its ``turn_status`` and its input:

    * ENDED: the lease is dropped as the ENDED exit does (a stale ``turn_status == "running"`` healed, the same
      outcome), and the input is not answered;
    * PAUSED: the no-op with the pause exit's outcome (``preserve_park=True``: a park is kept for /resume, and the
      adapter still applies the lost ``turn_no`` bump on success), leaving the input for /resume (a stale ``running``
      is healed and a stale ``interrupt_requested`` cleared, as the pause exit does);
    * RUNNING or WAITING: the rules above;
    * CREATED (a reset or a stale write): ``None``, the turn runs, as it did before this guard (the pool does not
      re-arm a CREATED row, so an input left on it would wait for ever);
    * GONE (deleted): the vanished-before-dispatch outcome of ``run_one_session_turn`` (``success=False,
      drop_lease=True``), logged as a vanished row. There is nothing to answer and nothing to bump.

    The same rule applies after an arming patch the row refuses (another process changed it under the patch): the
    row is read again under the same lock acquisition and judged the same way (RUNNING or WAITING: ``None``, the turn
    runs and answers the input now). A ``NotFoundError`` from the patch is a gone row; any other storage error from
    it, and a log that cannot be read, return ``None``: the turn runs, as it did before this guard.

    A PAUSED or ENDED release counts in ``session_completed_turn_noop_total`` and logs the no-op WARNING; neither runs
    the drain checkpoint. A gone row is not a completed-turn no-op and is not counted. The guard never swallows a turn
    it cannot see or cannot hand on.
    """
    async with session_lifecycle_lock().acquire(session_id):
        fresh = await session_storage.get(session_id)
        if fresh is None:
            return _drop_vanished_row(session_id)
        if fresh.completed_turn_no is None or fresh.completed_turn_no != fresh.turn_no:
            return None
        if fresh.status in _SETTLED_STATUSES:
            return await _release_settled_row(session_storage, fresh, "at the guard's first read")
        # The pool re-arms only a RUNNING or WAITING row (``_maybe_rearm_session``). Input waiting on a CREATED row
        # (after a reset or a stale write) would never be claimed again, so there the turn runs, as it did before
        # this guard.
        rearmable = fresh.status in _REARMABLE_STATUSES
        if fresh.turn_status == "claimable":
            if not rearmable:
                return None
            work = "a queued steer is armed"
        else:
            lines = await _read_message_lines(deps.workspace_io, fresh)
            if lines is None:
                return None
            if has_open_turn(lines, cursor=fresh.next_unprocessed_seq):
                if not rearmable:
                    return None
                try:
                    armed = await session_storage.patch_if(
                        session_id, {"turn_status": "claimable"},
                        where={"turn_status": ["idle", "running"], "status": _REARMABLE_STATUSES},
                    )
                except NotFoundError:
                    return _drop_vanished_row(session_id)
                except Exception:  # noqa: BLE001 - cannot arm it: answer it now instead of stranding it
                    logger.warning(
                        "session %s: could not arm the unanswered input of completed turn %d; running the turn",
                        session_id, fresh.turn_no, exc_info=True,
                    )
                    return None
                if armed is not None:
                    work = "an unanswered input was armed"
                else:
                    # The row changed under the patch (another process: this lock is per-process). Judge the row as
                    # it is now, under the same lock acquisition.
                    now = await session_storage.get(session_id)
                    if now is None:
                        return _drop_vanished_row(session_id)
                    if now.status in _SETTLED_STATUSES:
                        return await _release_settled_row(session_storage, now, "under the arming patch")
                    return None   # still RUNNING or WAITING (or CREATED): answer it now
            else:
                work = None
    if work is None:
        await _apply_pending_switch_at_checkpoint(deps, fresh)
        await _realize_pending_at_checkpoint(deps, fresh)
    _metrics.session_completed_turn_noop_total.inc()
    logger.warning(
        "session %s: turn %d already completed but its release never committed; releasing it without calling "
        "the model again (%s)",
        session_id, fresh.turn_no, work or "no unanswered input",
    )
    return ReleaseOutcome(success=True, drop_lease=True)


def _drop_vanished_row(session_id: str, where: str = "under the completed-turn guard") -> ReleaseOutcome:
    """The outcome of a claim whose row was deleted: the vanished-before-dispatch exit of ``run_one_session_turn``."""
    logger.warning("session %s vanished %s; dropping the lease", session_id, where)
    return ReleaseOutcome(success=False, drop_lease=True)


async def _settled_release(session_storage, row: WorkspaceSession) -> ReleaseOutcome:
    """The release of a claim whose row another process PAUSED or ENDED (called under the lifecycle lock).

    ENDED returns the ENDED exit's outcome and PAUSED the pause exit's (``preserve_park=True``: the park is kept for
    a later /resume, and the adapter still applies the lost ``turn_no`` bump once on success). A stale
    ``turn_status == "running"`` is healed on both, a stale ``interrupt_requested`` cleared on PAUSED (the pause
    exit does both: it must not leak into the turn that eventually resumes the row and downgrade a later Cancel to a
    Stop).
    """
    if row.turn_status == "running":
        await _clear_turn_running(session_storage, row.id)
    paused = row.status == SessionStatus.PAUSED
    if paused:
        await _clear_interrupt_requested(session_storage, row.id)
    # A PAUSED row gets the pause exit's release: its park is kept for /resume. preserve_park does not block the
    # turn_no bump (the adapter bumps on success in that branch too), so the lost bump is still applied once.
    return ReleaseOutcome(success=True, drop_lease=True, preserve_park=paused)


async def _release_settled_row(session_storage, row: WorkspaceSession, where: str) -> ReleaseOutcome:
    """The completed-turn guard's no-op release of a row another process PAUSED or ENDED: :func:`_settled_release`,
    counted as a no-op and logged. The input is not answered and the drain checkpoint does not run."""
    outcome = await _settled_release(session_storage, row)
    paused = row.status == SessionStatus.PAUSED
    _metrics.session_completed_turn_noop_total.inc()
    logger.warning(
        "session %s: turn %d already completed but its release never committed; releasing it without calling "
        "the model again (the row was %s %s%s)",
        row.id, row.turn_no, "paused" if paused else "ended", where,
        "; its input is left for /resume" if paused else "",
    )
    return outcome


# A refused running flip is tried again while the row reads live (a status that flipped paused and back between
# the write and the re-read); this many refusals in a row give the claim back unrun.
_RUNNING_FLIP_ATTEMPTS = 3


def _failure_code(exc: BaseException) -> str:
    """Why the turn failed, in one code.

    A stream that failed carries its own (``TurnStreamFailure.ended_detail_code``: the stream's code, else ``llm_stream_error``). A model call
    that RAISED before a stream opened (the adapter's classified error, e.g. an upstream 500 after the retries) carries it on the exception
    (``ServerError.code == "server_error"``); one that names no code is classified by its CLASS (rate limit, server, network, timeout). Anything
    else, or a generic ``PrimerError`` with no code, is ``turn_failed``: a turn that raised something that is not known to be a model error (an
    MCP toolset's ``NetworkError`` escaping ``list_tools`` is reported the same way as the model's). The code of a raised error is not written as ``ended_detail`` (it never was): it is the row's
    ``last_turn_error``, the event, and the rule for whether an interactive session rests.
    """
    if isinstance(exc, _NamesWhyItEndedTheTurn):
        return exc.ended_detail_code
    if not isinstance(exc, PrimerError):
        return "turn_failed"
    if isinstance(exc.code, str) and exc.code:
        return exc.code
    return next((code for cls, code in _TRANSPORT_CODE_BY_CLASS if isinstance(exc, cls)), "turn_failed")


async def _record_last_turn_error(session_storage, session_id: str, code: str, binding_epoch: int) -> bool:
    """Stamp ``last_turn_error`` (the failure's code and time) on the row: ONE ``patch_if`` of that field, guarded on the row not being ENDED and
    on the binding epoch the turn started under (a binding that switched while the turn ran is not this turn's failure to record).

    ``code`` is :func:`_failure_code`. Advisory: the failure exit's job is to release the lease, so a write that cannot land is logged and the exit
    goes on. Returns whether it landed: a session may REST only with its stamp (without one, a rested first-turn failure looks like a session that
    never started). Called under the lifecycle lock, before the status transition. Cleared by :func:`_flip_to_running` at the next turn.
    """
    patch = to_jsonable_python({"last_turn_error": {"code": code, "at": _now()}})
    try:
        written = await session_storage.patch_if(
            session_id, patch,
            # like the transition right after it: a binding that switched while the turn ran is not this turn's failure to record
            where={"status": NON_ENDED_STATUSES(), "binding_epoch": [binding_epoch]},
        )
    except NotFoundError:
        logger.warning("session %s vanished before its failed turn could be recorded on the row", session_id)
        return False
    except Exception:  # noqa: BLE001 -- advisory; the lease must still be released
        logger.exception("session %s: could not record the turn's failure (%s) on the row", session_id, code)
        return False
    return written is not None


async def _flip_to_running(
    session_storage, session: WorkspaceSession, stamp: datetime,
) -> tuple[WorkspaceSession | None, ReleaseOutcome | None]:
    """Mark ``session`` running for the turn about to start (called under the lifecycle lock).

    ONE ``patch_if`` of ``turn_status``, ``turn_started_at`` and the ``agent_phase`` stamps, guarded on the row not
    being PAUSED or ENDED (``NON_ENDED_STATUSES_NOT_PAUSED``); nothing else is written, so a wake or a flag another
    process committed since the top read survives. Returns ``(the row as written, None)`` when it landed. When it is
    refused the row is read again and judged: gone -> the vanished-before-dispatch outcome; ENDED or PAUSED -> the
    ENDED or pause exit's outcome (:func:`_settled_release`); live again -> tried again, and after
    ``_RUNNING_FLIP_ATTEMPTS`` refusals the claim is requeued unrun. Returns ``(None, the outcome)`` for every case
    that must not run the turn.
    """
    session_id = session.id
    patch = to_jsonable_python({
        "turn_status": "running",
        "turn_started_at": stamp,
        # agent_phase (01a04d91-a7a0, PHASE 1 of the execution-lifecycle revamp): "thinking" the instant the turn
        # is claimed, mirroring turn_status="running" in the same write. The streaming loop advances it on real
        # transitions (see infer_agent_phase); _clear_turn_running's callers reset it to None alongside
        # turn_status="idle" on every exit.
        "agent_phase": "thinking",
        "agent_phase_turn_no": session.turn_no,
        "agent_phase_stamped_at": stamp,
        # A turn that starts is past any earlier refusal of its workspace (ticket 01a1072f); the refusal arm writes it
        # again if this attempt is refused too.
        "workspace_refusal": None,
        # Likewise the last turn's failure (C-024): this turn is past it, and the failure exit writes it again if this one fails too.
        "last_turn_error": None,
    })
    for _ in range(_RUNNING_FLIP_ATTEMPTS):
        try:
            written = await session_storage.patch_if(
                session_id, patch, where={"status": NON_ENDED_STATUSES_NOT_PAUSED()},
            )
        except NotFoundError:
            return None, _drop_vanished_row(session_id, "before the running flip")
        if written is not None:
            return written, None
        now = await session_storage.get(session_id)
        if now is None:
            return None, _drop_vanished_row(session_id, "before the running flip")
        if now.status in _SETTLED_STATUSES:
            logger.warning(
                "session %s: the row was %s after the turn read it; not running the turn",
                session_id, "paused" if now.status == SessionStatus.PAUSED else "ended",
            )
            return None, await _settled_release(session_storage, now)
    logger.error(
        "session %s: the running flip was refused %d times while the row read live each time; giving the claim "
        "back unrun",
        session_id, _RUNNING_FLIP_ATTEMPTS,
    )
    return None, ReleaseOutcome(
        success=False, requeue_after=timedelta(seconds=1),
        last_error="the running flip was refused while the row read live",
    )


# The statuses another process can have settled under a completed turn's claim: the claim neither answers input on
# them nor runs a turn.
_SETTLED_STATUSES = (SessionStatus.PAUSED, SessionStatus.ENDED)

# The statuses ``WorkerPool._maybe_rearm_session`` re-arms a claimable row in.
_REARMABLE_STATUSES = [SessionStatus.RUNNING.value, SessionStatus.WAITING.value]


async def _read_message_lines(workspace_io, row: WorkspaceSession) -> list[str] | None:
    """The session's ``messages.jsonl`` lines, or ``None`` when they cannot be read (logged).

    Through the worker's IO shim (``read_state_file``, which resolves the workspace's own state path and returns
    nothing for an absent file) or a test fake's ``read_lines``. Bounded like the other workspace I/O of a turn exit.
    """
    try:
        async with asyncio.timeout(_BEST_EFFORT_IO_TIMEOUT_S):
            read_state_file = getattr(workspace_io, "read_state_file", None)
            if read_state_file is not None:
                raw = await read_state_file(row.workspace_id, f"sessions/{row.id}/messages.jsonl")
                text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
                return text.splitlines()
            read_lines = getattr(workspace_io, "read_lines", None)
            if read_lines is None:
                raise TypeError(f"{type(workspace_io).__name__} cannot read a message log")
            return list(read_lines(row.id))
    except Exception:  # noqa: BLE001 - the caller falls back to running the turn
        logger.warning(
            "session %s: could not read messages.jsonl to tell whether input is unanswered", row.id, exc_info=True,
        )
        return None


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


def _snapshot_resolver(storage_provider):
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
                return await storage_provider.get_storage(Graph).get(
                    binding.graph_id
                )
            return await storage_provider.get_storage(Agent).get(
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
    await apply_queued_binding_switch(
        storage_provider=deps.storage_provider, workspace_io=deps.workspace_io, session_id=session.id,
    )


async def apply_queued_binding_switch(
    *, storage_provider, workspace_io, session_id: str, guard: Mapping[str, Sequence[Any]] | None = None,
) -> None:
    """Apply the switch queued on a session's row, if any. Best effort: a failure is logged and the request stays queued.

    The body of the drain checkpoint, callable without a ``SessionDispatchDeps``: the pool's ``_end_session`` ends a
    session whose resume failed, and no checkpoint runs on that exit, so it applies the switch itself (a switch
    queued on a parked session otherwise survives on the ENDED row and the user's next message, after a reopen, is
    answered by the OUTGOING binding).

    Under ``session_lifecycle_lock``, with the row read INSIDE it (the switch is decided from that read, so a steer or a
    park that landed before the lock is seen), and the in-lock snapshot resolve, marker append and flush bounded by
    ``mutation_lock.IN_LOCK_IO_TIMEOUT_S``: an unreachable workspace must not hold the lock Cancel's closure and every steer
    need. A timeout leaves the switch pending (and a reserved gap in the seqs); a rejected reservation (the row changed:
    a steer took the next seq, a park committed) writes nothing and the switch stays pending for the next checkpoint.
    The queued-steer realize that follows is NOT called inside this lock: ``wake_session`` takes it itself.

    ``guard`` is the precondition on the row (default ``{"parked_status": [None]}``: a turn that ran has no park). The
    pool's ``_end_session`` passes ``{"status": ["ended"]}`` instead: it ends a session whose park columns are still set
    (the claim's release clears them later), so a "no park" guard would always reject there.
    """
    from primer.session import mutation_lock

    try:
        sessions = storage_provider.get_storage(WorkspaceSession)
        async with session_lifecycle_lock().acquire(session_id):
            fresh = await sessions.get(session_id)
            if fresh is None or fresh.pending_binding_switch is None:
                return
            from primer.session.binding_switch import apply_binding_switch

            try:
                async with asyncio.timeout(mutation_lock.IN_LOCK_IO_TIMEOUT_S):
                    await apply_binding_switch(
                        sessions=sessions,
                        workspace_io=workspace_io,
                        row=fresh,
                        request=fresh.pending_binding_switch,
                        actor=str(fresh.pending_binding_switch.get("actor") or "system"),
                        resolve_snapshot=_snapshot_resolver(storage_provider),
                        guard=guard if guard is not None else {"parked_status": [None]},
                    )
            except TimeoutError:
                logger.error(
                    "applying a queued binding switch timed out after %ss for %s; it stays queued for the next "
                    "checkpoint", mutation_lock.IN_LOCK_IO_TIMEOUT_S, session_id,
                )
    except Exception:
        logger.exception(
            "applying a queued binding switch failed for %s; it stays queued for the next checkpoint",
            session_id,
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
    await realize_queued_steer(
        storage_provider=deps.storage_provider,
        workspace_id=session.workspace_id,
        session_id=session.id,
        scheduler=deps.scheduler,
        claim_engine=deps.claim_engine,
        workspace_registry=deps.workspace_registry,
        event_bus=deps.event_bus,
    )


async def realize_queued_steer(
    *, storage_provider, workspace_id: str, session_id: str, scheduler, claim_engine, workspace_registry,
    event_bus=None,
) -> None:
    """Realize the oldest steer queued on a session, if any. Best effort: a failure is logged and the steer stays queued.

    The body of the drain checkpoint, callable without a ``SessionDispatchDeps``: the pool's ``_end_session`` ends a
    session (a failed resume, a cancelled park, a finished graph resume) with no turn behind it and so no checkpoint, and
    a steer queued on that session (``route_steer`` counts a parked session as busy) would otherwise wait for some LATER
    message to reopen it, or forever. A no-op when the wiring a wake needs (scheduler, claim engine, workspace registry)
    is absent.
    """
    if scheduler is None or claim_engine is None:
        return
    if workspace_registry is None:
        return
    try:
        wake_deps = SessionWakeDeps(
            storage_provider=storage_provider,
            scheduler=scheduler,
            claim_engine=claim_engine,
            workspace_registry=workspace_registry,
            event_bus=event_bus,
        )
        await realize_next_pending(
            storage_provider=storage_provider,
            workspace_id=workspace_id,
            session_id=session_id,
            wake_deps=wake_deps,
        )
    except Exception:
        logger.exception(
            "drain checkpoint: realizing a queued steer failed for %s",
            session_id,
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


async def clear_interrupt_for_resume(session_storage, session_id: str) -> None:
    """Drop a Stop that was recorded before a park is resumed.

    A later explicit human action (an approval, an answer) wins over an earlier Stop. The
    ``/interrupt`` route refuses a Stop on a parked session, so a flag can only be on the row here
    through a race with the park itself; left alone it would be honoured by the first poll of the
    turn that runs after the resume and kill the continuation before its first token. Taken under
    the lifecycle lock like every other write of this flag.
    """
    async with session_lifecycle_lock().acquire(session_id):
        await _clear_interrupt_requested(session_storage, session_id)


async def pause_session_for_refused_workspace(
    session_storage, session_id: str, refused: WorkspaceRefusedError,
) -> ReleaseOutcome:
    """Fail the turn of a session whose workspace the deployment refuses, and leave the session resumable.

    The session goes PAUSED (the existing ``/resume`` re-arms it), carrying the refusal text on the row because
    ``messages.jsonl`` lives INSIDE the refused workspace and cannot hold it. The turn stamps are cleared so nothing
    re-claims it in a loop, and an ENDED row is never brought back. Never ``workspace_lost``: the workspace is intact.

    ONE ``patch_if`` of exactly the fields this owns (the status, the reason, the Stop flag, the turn stamps), guarded on
    the row not being ENDED: never a write of a copy of the row it read, which would put back whatever another process
    committed since (a steer's ``last_seq``, a wake's ``turn_status``). ``turn_status`` goes to ``idle`` even where a
    wake had armed it ``claimable``: the pool re-arms only a RUNNING or WAITING row, and ``/resume`` claims the session
    whatever that flag says, so a PAUSED row has no use for it.

    The lease is given back with ``entity_noop``: the engine does not call the session adapter's release, which would
    clear the park, bump ``turn_no`` for a turn that never ran and try to write its error record into the very
    workspace that was refused.
    """
    logger.warning(
        "session %s: the deployment refuses its workspace, so the turn failed and the session is paused, "
        "resumable: %s", session_id, refused.message,
    )
    patch = to_jsonable_python({
        "status": SessionStatus.PAUSED,
        "workspace_refusal": refused.message,
        "interrupt_requested": False,
        "turn_status": "idle",
        "turn_started_at": None,
        "agent_phase": None,
        "agent_phase_turn_no": None,
        "agent_phase_stamped_at": None,
    })
    async with session_lifecycle_lock().acquire(session_id):
        try:
            await session_storage.patch_if(session_id, patch, where={"status": NON_ENDED_STATUSES()})
        except NotFoundError:
            logger.warning("session %s vanished before its refused turn could pause it", session_id)
    return ReleaseOutcome(success=False, drop_lease=True, entity_noop=True)


async def _clear_turn_running(session_storage, session_id: str) -> None:
    """Best-effort reset of ``turn_status``/``turn_started_at`` (and the
    finer-grained ``agent_phase``/``agent_phase_turn_no``/
    ``agent_phase_stamped_at``) to idle.

    The counterpart to the "set running" write made right before
    build_executor (step 2). Called from the finally block guarding the
    streaming phase (covers park/error/cancel/clean-completion - the four
    ways a turn that reached streaming can end) and from both pre-streaming
    exits (build-executor failure, executor-is-None) that return before
    that finally is ever reached.

    Only clears when turn_status is CURRENTLY "running" - never when it
    reads "claimable" - so a wake_session() that raced in a fresh claim
    during this turn's own cleanup is never stomped back to idle and
    stranded (the exact bug the claimable-consume step earlier in this
    function exists to avoid). Always called from inside a
    ``session_lifecycle_lock`` critical section, matching every other
    read-modify-write in this module - belt-and-suspenders alongside the
    ``update_unless`` guard below, not a substitute for it: the
    ``fresh.turn_status == "running"`` check is a CHEAP EARLY EXIT on a
    snapshot (mirrors primer.session.yields.durably_mark_session_resumable's
    own two-layer pattern - see its docstring), not the safety guarantee
    itself. The actual guarantee is ``update_unless``, which the backend
    evaluates against the row's CURRENT ``turn_status`` in the same
    statement as the write - so even a wake_session() that lands in the
    gap between this function's own read and write (the lock reduces but
    does not by itself prove there is no such gap across every caller)
    is still caught atomically, not by a stale Python-side snapshot.

    A worker that hard-crashes (OOM kill, pod eviction) between the
    "running" write and reaching either of these cleanup points never
    calls this - the row is left at turn_status="running" with a real
    turn_started_at. primer.workspace.session_reconcile.
    reconcile_sessions_to_workspace_lost covers the case where the crash
    took the workspace down with it (unconditional reset, since the
    workspace being gone makes any value moot); a worker crash that
    leaves the workspace reachable is not covered by any reconciler today
    and would need a dedicated lease-staleness sweep to catch.
    """
    fresh = await session_storage.get(session_id)
    if fresh is None or fresh.turn_status != "running":
        return
    updated = fresh.model_copy(update={
        "turn_status": "idle",
        "turn_started_at": None,
        # agent_phase is scoped to "while a turn is genuinely running"
        # (its own docstring) - clear it in the same write, same guard,
        # so it can never survive past the turn_status it's a
        # finer-grained sub-state of.
        "agent_phase": None,
        "agent_phase_turn_no": None,
        "agent_phase_stamped_at": None,
    })
    await session_storage.update_unless(
        updated, field="turn_status", forbidden="claimable",
    )


async def _write_agent_phase(
    session_storage, session_id: str, turn_no: int, phase: str,
) -> None:
    """Best-effort agent_phase row write (01a04d91-a7a0).

    No session_lifecycle_lock here, unlike every other read-modify-write
    in this module: agent_phase/agent_phase_turn_no/agent_phase_stamped_at
    have exactly ONE writer for the lifetime of a turn (this dispatch
    call) - nothing else ever touches THOSE THREE FIELDS. But
    model_copy(update=...) + a plain update() replaces the WHOLE row, so
    a stale ``fresh`` snapshot still risks reverting a DIFFERENT field a
    concurrent writer touched in the gap between this function's own read
    and write - most notably wake_session() flipping turn_status to
    "claimable" mid-turn (the exact hazard _clear_turn_running's own
    docstring documents). Guarding with ``update_unless`` closes that: the
    backend evaluates "is turn_status currently claimable" against the
    row's CURRENT value in the same statement as the write, so a raced-in
    claimable is never silently reverted back to whatever ``fresh`` saw
    turn_status as - mirrors primer.session.yields' established pattern for
    this exact class of race (see durably_mark_session_resumable's
    docstring). A rejected write (turn_status already claimable) is a
    silent no-op here: the turn is ending/being steered either way, so
    one skipped phase transition is harmless - the row already reads
    "claimable", not something the phase field could make more correct.
    """
    fresh = await session_storage.get(session_id)
    if fresh is None:
        return
    updated = fresh.model_copy(update={
        "agent_phase": phase,
        "agent_phase_turn_no": turn_no,
        "agent_phase_stamped_at": _now(),
    })
    try:
        await session_storage.update_unless(
            updated, field="turn_status", forbidden="claimable",
        )
    except Exception:  # noqa: BLE001 - best-effort, never block the turn
        logger.exception(
            "session %s: failed to write agent_phase=%r", session_id, phase,
        )


class _TerminalWrite(NamedTuple):
    """What a turn's terminal write left on the row, for the callers that ANNOUNCE the outcome.

    ``landed`` is True when this write changed the row. ``status`` / ``ended_reason`` are what the row now
    says: the outcome the turn asked for when it landed or when there was nothing to protect (an identical
    repeat, an epoch-voided write, a vanished row), and the row's OWN outcome when the row was already
    ENDED and the write was skipped. A caller announces these, not the outcome it computed, so the durable
    event log cannot contradict the row.
    """

    landed: bool
    status: SessionStatus
    ended_reason: str | None


# The ended reasons the on-disk AgentSession slot accepts (see AgentSession.set_status).
_SLOT_ENDED_REASONS = ("completed", "failed", "cancelled", "tool_turn_cap")


async def _leave_ended_row_alone(
    session: WorkspaceSession,
    ended: WorkspaceSession,
    new_status: SessionStatus,
    ended_reason: str | None,
    *,
    executor: Any,
    workspace_registry: Any | None,
) -> _TerminalWrite:
    """The first terminal reason wins: report the row's own outcome instead of writing the turn's.

    Something else ended this session while the turn ran (a force-delete wrote ENDED/force_deleted, the pool's
    preempt convergence ENDED/cancelled, the reconciler ENDED/workspace_lost) and the turn's own outcome
    arrives after it. Writing it would hide why the session ended, resurrect the row (a WAITING over an ENDED
    one) and mirror the wrong reason onto the on-disk slot. Every caller is a turn writing ITS outcome; none
    reopens a session (that is wake_session's job, not a turn's).

    The slot is brought in line with the ROW, not left as it was: the pool's ``_end_session`` ends a row without
    touching the slot, so with the turn's own mirror skipped nothing else would take ``session.json`` out of
    RUNNING. Only a reason the slot accepts is mirrored (``force_deleted`` and ``workspace_lost`` are not: the
    slot of a deleted session is being removed, and an unknown reason would be written as ``completed``).
    """
    logger.info(
        "session %s: the row is already ENDED (%s); not overwriting it with %s/%s",
        session.id, ended.ended_reason, new_status.value, ended_reason,
    )
    await _mirror_ended_row_onto_slot(session, ended, executor=executor, workspace_registry=workspace_registry)
    return _TerminalWrite(False, SessionStatus.ENDED, ended.ended_reason)


async def _mirror_ended_row_onto_slot(
    session: WorkspaceSession,
    ended: WorkspaceSession,
    *,
    executor: Any,
    workspace_registry: Any | None,
) -> None:
    """Bring the on-disk slot in line with an ENDED row's OWN reason, when the slot accepts that reason.

    The one slot policy for a row that was ended by something else: the terminal write's skip
    (:func:`_leave_ended_row_alone`) and the cancelled exit's early branch both use it, so the same row cannot get
    a mirror or not depending on which side of a race the turn saw it on. Bounded by ``_SLOT_MIRROR_TIMEOUT_S``
    inside :func:`_sync_agent_session_ended`.
    """
    if ended.ended_reason in _SLOT_ENDED_REASONS:
        await _sync_agent_session_ended(
            executor, ended.ended_reason, session=session, workspace_registry=workspace_registry,
        )


async def _transition_session_status(
    session_storage,
    session: WorkspaceSession,
    *,
    new_status: SessionStatus,
    ended_reason: str | None = None,
    ended_detail: str | None = None,
    executor=None,
    expected_epoch: int | None = None,
    workspace_registry: Any | None = None,
) -> _TerminalWrite:
    """Update the WorkspaceSession row in storage. Idempotent on no-op.

    Returns what the row says afterwards (:class:`_TerminalWrite`); callers that announce the outcome use it.

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

    ``workspace_registry`` (01a06cbc) is the fallback for the one caller
    that has no ``executor`` at all -- a ``build_executor`` failure, which
    means no executor object was ever created. See
    :func:`_sync_agent_session_ended`'s docstring for why the slot is
    still reachable in that case.
    """
    # Re-read the current row so we don't overwrite concurrent changes.
    fresh = await session_storage.get(session.id)
    if fresh is None:
        return _TerminalWrite(False, new_status, ended_reason)
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
        return _TerminalWrite(False, new_status, ended_reason)
    if fresh.status == new_status and (
        ended_reason is None or fresh.ended_reason == ended_reason
    ):
        return _TerminalWrite(False, new_status, ended_reason)
    if fresh.status == SessionStatus.ENDED:
        return await _leave_ended_row_alone(
            session, fresh, new_status, ended_reason,
            executor=executor, workspace_registry=workspace_registry,
        )
    updates: dict[str, object | None] = {"status": new_status}
    if new_status == SessionStatus.ENDED:
        updates["ended_at"] = datetime.now(timezone.utc)
        if ended_reason is not None:
            updates["ended_reason"] = ended_reason
        if ended_detail is not None:
            updates["ended_detail"] = ended_detail
    # 01a08bf0: let a genuine storage failure propagate instead of swallowing
    # it. run_one_session_turn's single production caller
    # (WorkerPool._run_engine_session) pre-sets a safe
    # ReleaseOutcome(success=False, drop_lease=True) before its try and
    # releases with it in a finally regardless of what raises, so an
    # exception here converges on an honest failed-release rather than
    # stranding the lease -- and it means the ENDED-only code below (the
    # on-disk AgentSession mirror, and the caller's _publish_terminal) never
    # runs for a status this process never actually persisted.
    #
    # A CONDITIONAL write: the check above is on this helper's own snapshot, and the lifecycle lock does not
    # cover every writer. It is process-local (a force-delete on another API process does not serialize with
    # this worker), and the reconciler and the pool's _end_session do not take it at all, so the row can be
    # ended between that read and this write. The backend refuses the write in the same statement if the
    # stored status is ENDED.
    landed = await session_storage.update_unless(
        fresh.model_copy(update=updates), field="status", forbidden=SessionStatus.ENDED.value,
    )
    if landed is None:
        ended_now = await session_storage.get(session.id)
        if ended_now is None:
            return _TerminalWrite(False, new_status, ended_reason)
        return await _leave_ended_row_alone(
            session, ended_now, new_status, ended_reason,
            executor=executor, workspace_registry=workspace_registry,
        )
    if new_status == SessionStatus.ENDED:
        await _sync_agent_session_ended(
            executor, ended_reason,
            session=session, workspace_registry=workspace_registry,
        )
    return _TerminalWrite(True, new_status, ended_reason)


async def _rest_session(session_storage, session: WorkspaceSession, *, expected_epoch: int) -> bool:
    """Leave ``session`` RESTING after a failed turn: ONE field-scoped ``patch_if`` of ``status`` to WAITING. True when it landed.

    The write itself carries the conditions, so they hold on any process (the lifecycle lock is this one's): the row is still RUNNING or WAITING (not
    ended, not paused), no Cancel is pending (``cancel_requested`` is false: a row that rested with it set would end as cancelled, without calling the
    model, on the next send) and the binding epoch is the one the turn started under. A refused write means the session ends instead, as it did before
    a failure could rest.
    """
    try:
        written = await session_storage.patch_if(
            session.id, to_jsonable_python({"status": SessionStatus.WAITING}),
            where={
                "status": [SessionStatus.RUNNING.value, SessionStatus.WAITING.value],
                "cancel_requested": [False],
                "binding_epoch": [expected_epoch],
            },
        )
    except NotFoundError:
        return False
    return written is not None


async def _reopen_agent_session_slot(executor) -> None:
    """Put the executor's on-disk AgentSession slot (``session.json``) back to RUNNING after a failed turn left the session resting.

    ``WorkspaceAgentExecutor.invoke`` ends the slot on every failed turn; when the row ENDED too, the reopen path (``wake_session``'s ENDED branch)
    reopened it. A row that RESTS takes the in-place path, which never does. ``reopen`` is the one sanctioned way out of ENDED and a no-op on a slot
    that is not; bounded like the mirror in the other direction, since it commits over the workspace runtime connection under the lifecycle lock.
    Advisory: ``wake_session`` reopens a slot that still reads ENDED under a live row too.
    """
    inner = getattr(executor, "session", None)
    reopen = getattr(inner, "reopen", None)
    if reopen is None:
        return
    try:
        async with asyncio.timeout(_SLOT_MIRROR_TIMEOUT_S):
            if await inner.status() == SessionStatus.ENDED:
                await reopen()
    except TimeoutError:
        logger.warning(
            "dispatch: the AgentSession slot was not reopened within %gs (the workspace is not accepting writes); the row rests and the "
            "slot may still read ended until the next send reopens it", _SLOT_MIRROR_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 -- advisory; never block release
        logger.warning("dispatch: failed to reopen the AgentSession slot of a session left resting", exc_info=True)


async def _sync_agent_session_ended(
    executor,
    ended_reason: str | None,
    *,
    session: WorkspaceSession | None = None,
    workspace_registry: Any | None = None,
) -> None:
    """Mirror a terminal ENDED transition onto the on-disk AgentSession slot.

    Commits ``session.json`` (status=ENDED) so the workspace-side reads
    (``get_session`` / ``list_sessions``) agree with the scheduler row.
    Best-effort: a missing executor / already-ENDED slot / commit failure
    must never block the lease release, so every branch is swallowed with a
    log. ``ended_reason`` is constrained to the four terminal reasons the
    AgentSession transition table accepts (``completed``, ``failed``,
    ``cancelled``, ``tool_turn_cap``); an unknown value falls back to
    ``"completed"`` so the on-disk slot still reaches a terminal state. A
    reason missing from that list is a bug, not a harmless default: the MCP
    workspace tools read this slot, so they would report it as completed
    while REST reports the real reason.

    01a06cbc: when a ``build_executor`` failure means no executor was ever
    created, there is no ``.session`` to unwrap -- but the on-disk slot
    itself isn't a build_executor artifact at all. It was allocated by the
    REST API at session-creation time (``Workspace.start_session(...,
    id=sid)``); both ``build_agent_executor`` / ``build_graph_executor``
    just LOAD it mid-build, after the steps that actually fail (agent/LLM/
    toolset resolution, or graph/state_repo resolution). So its existence
    doesn't depend on the rest of the build succeeding, and it can be
    re-resolved independently from nothing but ``(workspace_id, session_id)``
    via ``workspace_registry`` -- the SAME primitive
    (``workspace.get_session(session.id)``) the builders already call.
    Only attempted when the executor path found nothing (so the three
    existing callers that already pass a real executor are unaffected). A
    graph-bound session predating holder allocation can have no slot at
    all (``get_session`` returns ``None``) -- that's a normal no-op here,
    same tolerance a missing executor already gets.
    """
    inner = getattr(executor, "session", None) if executor is not None else None
    if inner is None and workspace_registry is not None and session is not None:
        try:
            # Bounded like the mirror below: the registry and the workspace go over the same runtime
            # connection. A timeout is a TimeoutError, which the handler below logs and swallows.
            async with asyncio.timeout(_SLOT_MIRROR_TIMEOUT_S):
                workspace = await workspace_registry.get_workspace(session.workspace_id)
                inner = (
                    await workspace.get_session(session.id)
                    if workspace is not None else None
                )
        except Exception:  # noqa: BLE001 -- advisory; never block release
            logger.warning(
                "dispatch: failed to load the on-disk AgentSession slot "
                "for %s while mirroring ENDED (no executor was built)",
                session.id, exc_info=True,
            )
            return
    set_status = getattr(inner, "set_status", None)
    if set_status is None:
        return
    try:
        # Bounded: the slot commit goes over the workspace runtime connection, and on a Cancel this runs
        # inside the session's lifecycle lock. A dead connection would otherwise hold that lock forever.
        async with asyncio.timeout(_SLOT_MIRROR_TIMEOUT_S):
            current = await inner.status()
            if current == SessionStatus.ENDED:
                return
            reason = ended_reason if ended_reason in _SLOT_ENDED_REASONS else "completed"
            await set_status(SessionStatus.ENDED, ended_reason=reason)
    except TimeoutError:
        logger.warning(
            "dispatch: the ENDED status was not confirmed on the AgentSession slot within %gs (the "
            "workspace is not accepting writes); the row is ENDED and the slot may still read running",
            _SLOT_MIRROR_TIMEOUT_S,
        )
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
    *,
    session_storage: Any | None = None,
) -> None:
    """Set ``cancel_event`` when a Stop or Cancel is requested, from two independent sources.

    * The bus key ``session:{sid}:cancel``: the fast path, delivered in milliseconds.
    * The session row's ``interrupt_requested`` flag, polled every ``_INTERRUPT_POLL_S``
      (the first read is immediate): the durable fallback. The bus is not durable, so a Stop
      whose publish failed, a bus that cannot subscribe, a Stop recorded before this turn began
      and the window before the subscription is live would otherwise be lost silently, and
      nothing else reads the flag during a turn.

    Either source ending the watch ends the other. A bus that fails (it cannot subscribe, or its
    iterator dies) is logged and the poll carries on alone; before, it killed this task silently and
    every later Stop of the turn was lost. Without ``session_storage`` only the bus is watched.
    """
    watchers = [
        asyncio.create_task(
            _watch_bus_for_cancel(event_bus, session_id, cancel_event),
            name=f"sess-cancel-bus-{session_id}",
        )
    ]
    if session_storage is not None:
        watchers.append(
            asyncio.create_task(
                _poll_row_for_interrupt(session_storage, session_id, cancel_event),
                name=f"sess-cancel-poll-{session_id}",
            )
        )
    try:
        await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)


async def _watch_bus_for_cancel(
    event_bus: EventBus, session_id: str, cancel_event: asyncio.Event,
) -> None:
    try:
        sub = event_bus.subscribe()
    except Exception as exc:  # noqa: BLE001 - the row poll is the fallback
        logger.warning(
            "session %s: cannot subscribe to the bus for Stop/Cancel (%s); "
            "relying on the session row poll", session_id, exc,
        )
        await asyncio.Event().wait()      # stay quiet so the poll keeps the watch alive
        return
    try:
        async for event in sub:
            if event.event_key == f"session:{session_id}:cancel":
                cancel_event.set()
                return
    except asyncio.CancelledError:
        return
    except Exception as exc:  # noqa: BLE001 - the row poll is the fallback
        logger.warning(
            "session %s: the bus subscription for Stop/Cancel failed (%s); "
            "relying on the session row poll", session_id, exc,
        )
        await asyncio.Event().wait()
    finally:
        await sub.aclose()


async def _poll_row_for_interrupt(
    session_storage: Any, session_id: str, cancel_event: asyncio.Event,
) -> None:
    first_read = True
    while True:
        try:
            row = await session_storage.get(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retried next interval
            logger.warning(
                "session %s: could not read the row to look for a Stop (%s); will retry",
                session_id, exc,
            )
        else:
            if row is not None and row.interrupt_requested:
                if not cancel_event.is_set():
                    cancel_event.set()
                    # The first read is the turn's own start: a flag already there was recorded
                    # BEFORE the turn began (a queued Stop), which is not a bus fault. Only a flag
                    # that appears on a later read was requested while the turn ran and missed the
                    # bus, which is the case that means the bus is dropping Stops.
                    reason = "queued_before_turn" if first_read else "missed_while_running"
                    _metrics.session_interrupts_via_poll_total.labels(reason).inc()
                    logger.info(
                        "session %s: Stop reached this turn through the session row, not the bus (%s)",
                        session_id, reason,
                    )
                return
            first_read = False          # only a read that SUCCEEDED and found nothing is "the start"
        await asyncio.sleep(_INTERRUPT_POLL_S)


__all__ = ["SessionDispatchDeps", "run_one_session_turn"]
