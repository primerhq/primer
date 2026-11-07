"""Per-kind engine claim handlers for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). One
function per non-session claim kind (harness, memory, trigger, tool_call); each takes the
:class:`~primer.worker.pool.WorkerPool` instance as ``pool`` and reads the same
bound deps the original methods did (``pool._storage`` / ``pool._engine`` /
``pool._event_bus`` / ...). The pool keeps thin delegating methods
(``WorkerPool._run_engine_harness`` etc.) so the ``start()`` dispatch table
(``self._run_engine_harness``) and any test monkeypatches still resolve through
the instance.

The SESSION handler (``_run_engine_session``) intentionally stays in
``pool.py``: it is the ``run_one_session_turn`` monkeypatch seam
(``patch("primer.worker.pool.run_one_session_turn")``).

Per-kind dispatch imports stay lazy inside each function so importing this
module (and ``pool``) doesn't pull a kind's dependency
trees at startup — mirroring the original methods.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.int.claim import Lease as ClaimLease
    from primer.worker.pool import WorkerPool

logger = logging.getLogger(__name__)




async def run_engine_harness(pool: "WorkerPool", engine_lease: "ClaimLease") -> None:
    """Handle a HARNESS claim from the engine.

    Bridges to run_one_harness_operation via HarnessDispatchDeps.
    Stamps claimed_by on the harness row so heartbeat checks pass during
    long operations.
    """
    from primer.harness.dispatch import HarnessDispatchDeps, run_one_harness_operation
    from primer.int.claim import ReleaseOutcome
    from primer.model.harness import Harness

    # Verify the harness still has a pending operation before dispatching.
    harness_storage = pool._storage.get_storage(Harness)
    harness = await harness_storage.get(engine_lease.entity_id)
    if harness is None or harness.pending_operation is None:
        await pool._release_lease(
            engine_lease, ReleaseOutcome(success=False, drop_lease=True),
        )
        return

    deps = HarnessDispatchDeps(
        storage_provider=pool._storage,
        event_bus=pool._event_bus,
        provider_registry=pool._provider_registry,
        semantic_search_registry=pool._semantic_search_registry,
    )
    success = False
    try:
        await run_one_harness_operation(
            deps,
            harness_id=engine_lease.entity_id,
            worker_id=pool._worker_id,
        )
        success = True
    except Exception:
        logger.exception(
            "engine harness operation for %s raised",
            engine_lease.entity_id,
        )
    finally:
        await pool._release_lease(
            engine_lease, ReleaseOutcome(success=success, drop_lease=True),
        )


async def run_engine_memory(pool: "WorkerPool", engine_lease: "ClaimLease") -> None:
    """Handle a MEMORY claim: one background job of agent memory (reflect, curate or topics).

    ``run_memory_job`` holds the gates (missing agent, disabled, expired, over budget, trivial session) and answers what to do with the lease. A job
    that raises is dropped, never retried in a loop: its trigger (the next turn or the daily schedule) enqueues it again.
    """
    from primer.events.recorder import recorder_for
    from primer.int.claim import ReleaseOutcome
    from primer.knowledge.document_service import DocumentService
    from primer.knowledge.indexing import make_document_indexer, make_document_path_rewriter, make_document_unindexer
    from primer.memory.jobs import MemoryDeps, report_job_error, run_memory_job
    from primer.memory.reflect import session_log_shapes, session_message_loader
    from primer.memory.store import MemoryStore
    from primer.worker.io_shim import _WorkspaceIOShim

    outcome = ReleaseOutcome(success=False, drop_lease=True)
    deps = None
    try:
        recorder = recorder_for(pool._storage, pool._event_bus)
        documents = DocumentService(
            pool._storage,
            indexer=make_document_indexer(
                storage_provider=pool._storage, provider_registry=pool._provider_registry, semantic_search_registry=pool._semantic_search_registry,
            ),
            unindexer=make_document_unindexer(storage_provider=pool._storage, semantic_search_registry=pool._semantic_search_registry),
            path_rewriter=make_document_path_rewriter(storage_provider=pool._storage, semantic_search_registry=pool._semantic_search_registry),
        )
        read_log = _WorkspaceIOShim(workspace_registry=pool._workspace_registry).read_session_log
        deps = MemoryDeps(
            sp=pool._storage,
            provider_registry=pool._provider_registry,
            semantic_search_registry=pool._semantic_search_registry,
            engine=pool._engine,
            recorder=recorder,
            store=MemoryStore(pool._storage, documents, recorder),
            load_messages=session_message_loader(pool._storage, read_log),
            describe_log=session_log_shapes(pool._storage, read_log),
            now=lambda: datetime.now(timezone.utc),
        )
        outcome = await run_memory_job(deps, engine_lease)
    except Exception as exc:
        logger.exception("engine memory job %s raised", engine_lease.entity_id)
        if deps is not None:
            await report_job_error(deps, engine_lease, exc)
    finally:
        await pool._release_lease(engine_lease, outcome)


def _now() -> datetime:
    """The worker's clock when it handles a claim (a seam: the catchup tests run the trigger handler on a fake one)."""
    return datetime.now(timezone.utc)


def _clock_ticks(trigger, *, now: datetime) -> list[datetime | None]:
    """What one claim of ``trigger`` fires, in order: the ``scheduled_for`` of each fire; the last is the due tick (ticket 01a16ddb, rulings 3, 4).

    * ``scheduled``: the missed ticks its ``catchup`` keeps, then the due tick, each once
      (:func:`primer.trigger.cron.scheduled_ticks_to_fire`). A row whose cron or timezone cannot be read (both are validated when saved) fires
      once, for its ``next_fire_at``: a malformed row never blocks the fire.
    * ``delayed``: its ``fire_at``.
    * a row that is gone or disabled (``fire_trigger`` answers it skipped), or any other kind: one fire with no tick.

    ``[]`` when the claim is early: its tick (a ``scheduled`` trigger's ``next_fire_at``, a ``delayed`` one's ``fire_at``) is more than
    :data:`~primer.trigger.cron.CLOCK_SKEW` ahead of ``now``. A future tick is never fired early.
    """
    from primer.trigger.cron import CLOCK_SKEW, scheduled_ticks_to_fire

    if trigger is None or not trigger.enabled:
        return [None]
    kind = trigger.config.kind
    if kind == "delayed":
        fire_at = trigger.config.fire_at
        return [] if fire_at > now + CLOCK_SKEW else [fire_at]
    if kind != "scheduled":
        return [None]
    try:
        return list(scheduled_ticks_to_fire(
            trigger.config.cron,
            trigger.config.timezone,
            catchup=trigger.config.catchup,
            first_unfired=trigger.next_fire_at,
            now=now,
        ))
    except Exception:
        logger.exception(
            "trigger %s: could not work out its ticks; firing once for its next_fire_at",
            trigger.id,
        )
        return [trigger.next_fire_at]


def _not_fired_yet(trigger, ticks: list[datetime | None]) -> list[datetime | None]:
    """``ticks`` without the ones a delivery of this claim already fired (ticket 01a16ddb, review of #915).

    A claim whose release was lost (a worker that died after its fires) is delivered again with ``next_fire_at`` unchanged, so it plans the
    same ticks. ``fire_trigger`` records the ``fire_id`` of the last tick it fired (``last_fired_id``), and the ticks are fired oldest first:
    when that id is one of these ticks', the tick and every tick before it already fired, and only the ticks after it are left. Read once,
    from the row the claim read.
    """
    from primer.trigger.fire_id import make_fire_id

    last = getattr(trigger, "last_fired_id", None)
    if last is None:
        return ticks
    ids = [make_fire_id(trigger.id, tick) if tick is not None else None for tick in ticks]
    if last not in ids:
        return ticks
    return ticks[ids.index(last) + 1:]


async def _lease_after_the_fire(
    pool: "WorkerPool", engine_lease: "ClaimLease", *, fired_tick: datetime | None, success: bool,
) -> tuple[bool, timedelta | None]:
    """How a claim that fired gives its lease back: ``(drop_lease, requeue_after)`` (ticket 01a16ddb, review of #915).

    The lease is requeued to the trigger's next tick, the one the release records (``TriggerClaimAdapter.on_release``: the next cron tick
    after ``fired_tick``, the claim's due tick, for a ``scheduled`` trigger; after now without one), so it is not claimable again before it, and
    is claimable at once when that tick passed during the fire (W47). Without the requeue the in-memory claim engine, which did
    not evaluate the adapter's eligibility then, re-claimed a released trigger at once, and every scheduled trigger re-fired in a loop on a
    SQLite deployment; it now applies the trigger's eligibility too (ticket 01a16e79), and the requeue keeps the lease out of each claim's
    entity reads until its tick. A ``delayed`` trigger whose fire raised before it finished stays armed (the release leaves it), so its lease is
    requeued with the worker's retry backoff (W48). A trigger with no next tick (a ``delayed`` trigger that fired, a disabled or deleted one, an unreadable cron) drops the
    lease: the save that lets it fire again upserts a new one (``primer.trigger.service._upsert_claim_if_eligible``). A row that cannot be
    read leaves the lease as the engine would.
    """
    from primer.claim.adapters.triggers import delayed_fire_finished
    from primer.model.trigger import Trigger
    from primer.trigger.cron import next_fire_at

    trigger_id = engine_lease.entity_id
    try:
        trigger = await pool._storage.get_storage(Trigger).get(trigger_id)
    except Exception:
        logger.exception("trigger %s: could not read it to requeue its lease", trigger_id)
        return False, None
    if trigger is None or not trigger.enabled:
        return True, None
    if trigger.config.kind == "delayed" and not delayed_fire_finished(trigger, success=success):
        return False, timedelta(seconds=_tool_call_backoff_seconds(pool.config, engine_lease.attempt_count + 1))
    if trigger.config.kind != "scheduled":
        return True, None
    now = _now()
    try:
        upcoming = next_fire_at(trigger.config.cron, trigger.config.timezone, after=fired_tick or now)
    except Exception:
        return True, None
    return False, max(upcoming - now, timedelta(0))


async def run_engine_trigger(pool: "WorkerPool", engine_lease: "ClaimLease") -> None:
    """Handle a TRIGGER claim from the engine.

    Routes the lease to :func:`primer.trigger.dispatch.fire_trigger`,
    which fans out to each enabled subscription's dispatcher. The
    ``TriggerClaimAdapter.on_release`` hook advances ``next_fire_at``
    (cron tick for ``scheduled``, null/disabled for ``delayed``), and the
    lease is requeued to that next tick (:func:`_lease_after_the_fire`).

    Every fire carries the tick it is for as ``scheduled_for`` (:func:`_clock_ticks`). A ``scheduled`` trigger fires the missed ticks its
    ``catchup`` keeps (``none`` none, ``one`` the most recent, ``all`` each, at most 64, the oldest first), then the due tick once; a missed tick
    is one from the trigger's ``next_fire_at`` up to the due tick (ticket 01a16ddb, rulings 3 and 4). A claim delivered again after a lost
    release skips the ticks it already fired (:func:`_not_fired_yet`). A catch-up fire that raises is logged and the next one runs; the due
    tick's own errors reach the outer except.

    An early claim (its tick more than ``CLOCK_SKEW`` ahead of the worker's clock) fires nothing and hands the lease back untouched
    (``entity_noop``: the release must not advance or disable a trigger that did not fire), requeued to its tick.
    """
    from primer.int.claim import ReleaseOutcome
    from primer.model.trigger import Trigger
    from primer.trigger.dispatch import fire_trigger
    from primer.trigger.subscribers import DispatchDeps

    deps = DispatchDeps(
        storage_provider=pool._storage,
        claim_engine=pool._engine,
        scheduler=pool._scheduler,
        workspace_registry=getattr(pool, "_workspace_registry", None),
        event_bus=pool._event_bus,
    )

    def _failed_deliveries(fire_result) -> int:
        # fire_trigger isolates every per-subscription failure and
        # RETURNS it (ok=False) instead of raising, so "it returned"
        # says nothing about whether anything was delivered.
        return sum(
            1 for r in getattr(fire_result, "results", None) or []
            if not r.get("ok")
        )

    success = False
    failed_deliveries = 0
    replay_errors = 0
    last_error: str | None = None
    early_until: datetime | None = None
    due_tick: datetime | None = None
    now = _now()
    try:
        trigger = await pool._storage.get_storage(Trigger).get(engine_lease.entity_id)
        ticks = _clock_ticks(trigger, now=now)
        if not ticks:
            early_until = trigger.config.fire_at if trigger.config.kind == "delayed" else trigger.next_fire_at
            logger.info(
                "trigger %s: claimed before its tick %s; firing nothing, requeued to it",
                engine_lease.entity_id, early_until.isoformat(),
            )
            return
        # The release advances from it, even when an earlier delivery of this claim already fired it (W47).
        due_tick = ticks[-1]
        ticks = _not_fired_yet(trigger, ticks)
        for position, tick in enumerate(ticks):
            if position < len(ticks) - 1:
                try:
                    replayed = await fire_trigger(
                        trigger_id=engine_lease.entity_id,
                        scheduled_for=tick,
                        deps=deps,
                    )
                    failed_deliveries += _failed_deliveries(replayed)
                except Exception:
                    replay_errors += 1
                    logger.exception(
                        "trigger %s: catchup fire at %s raised; "
                        "skipping to next",
                        engine_lease.entity_id, tick.isoformat() if tick is not None else None,
                    )
                continue
            current = await fire_trigger(
                trigger_id=engine_lease.entity_id,
                scheduled_for=tick,
                deps=deps,
            )
            failed_deliveries += _failed_deliveries(current)
        if failed_deliveries or replay_errors:
            # Honest claim-row bookkeeping only: TriggerClaimAdapter
            # advances next_fire_at whatever the outcome, so this does
            # not retry anything. The findable record of each failed
            # delivery is the trigger.delivery_failed event plus the
            # subscription's own last_fire_error (primer/trigger/
            # dispatch.py); this just stops the release reporting a
            # clean run over them.
            last_error = (
                f"{failed_deliveries} subscription delivery(ies) failed, "
                f"{replay_errors} catchup fire(s) raised"
            )
            logger.warning(
                "trigger %s: %s (see trigger.delivery_failed events)",
                engine_lease.entity_id, last_error,
            )
        else:
            success = True
    except Exception:
        logger.exception(
            "engine trigger fire for %s raised",
            engine_lease.entity_id,
        )
    finally:
        if early_until is not None:
            outcome = ReleaseOutcome(
                success=True, entity_noop=True, requeue_after=max(early_until - now, timedelta(0)),
            )
        else:
            drop, requeue_after = await _lease_after_the_fire(pool, engine_lease, fired_tick=due_tick, success=success)
            outcome = ReleaseOutcome(
                success=success, drop_lease=drop, requeue_after=requeue_after, last_error=last_error, fired_tick=due_tick,
            )
        await pool._release_lease(engine_lease, outcome)


# ---- the TOOL_CALL executor (BPF wave 1; spec: A2 analysis section 6(a)) ---------

# The global yield cap a task gate falls back to when the yielding tool named no
# timeout of its own: the same 3600 s cap the session's own park arms (the
# ToolWaitPark handler's fallback in primer.session.dispatch).
_TASK_GATE_DEFAULT_TIMEOUT_SECONDS = 3600.0

# A bounded retry for the step-2 fenced patch: the only writer that can move the
# row between the step-1 read and the patch is a STALE release from the previous
# holder (fenced on its own token), and that write is one statement.
_CLAIM_ROW_MAX_ATTEMPTS = 3


class _WorkspaceReadFileIO:
    """The read side of ``primer.tap.reader._WorkspaceReadIO`` bound to one live workspace.

    ``read_record_by_seq`` builds the session's directory from the ``state_path`` attribute and lists and reads the log's segments through
    ``list_files``, ``file_info``, ``read_file`` and ``read_file_from``; all delegate to the workspace backend, the same seam the pool's own
    write-side shim (``_WorkspaceIOShim.read_state_file``) uses, so a workspace that overrides its state path via a template is honoured on both sides.
    """

    def __init__(self, workspace) -> None:
        self._workspace = workspace
        self.state_path = getattr(workspace, "state_path", ".state")

    async def list_files(self, path: str = "."):
        return await self._workspace.list_files(path)

    async def file_info(self, path: str):
        return await self._workspace.file_info(path)

    async def read_file(self, path: str) -> bytes:
        return await self._workspace.read_file(path)

    async def read_file_from(self, path: str, offset: int) -> bytes:
        return await self._workspace.read_file_from(path, offset)


def _tool_call_backoff_seconds(config, failures: int) -> float:
    """The requeue delay after the failures-th failed attempt: base * 2 ** (n-1),
    capped. The first failure waits one base interval; the cap is the config's
    own max_backoff_seconds (D-A2-03's retry cap consumes the fields the config
    already documented)."""
    delay = config.base_backoff_seconds * (2 ** (max(1, failures) - 1))
    return min(delay, config.max_backoff_seconds)


def _short_error(exc: BaseException) -> str:
    """The row's last_error width: 300 chars, the class name when the message is empty."""
    text = str(exc)
    if not text:
        text = type(exc).__name__
    return text[:300]


async def run_engine_tool_call(pool: "WorkerPool", engine_lease: "ClaimLease") -> None:
    """Execute one claimed TOOL_CALL task: the claim-based tool-call executor.

    The C4 executor (A2 spec section 6(a), steps 1-8). The session's turn parked
    on this batch (``tool_calls_as_claims_enabled``); this handler claims the
    task, runs its one tool call through the session's own tool surface, and
    releases the lease through :meth:`WorkerPool._release_lease` (never
    ``engine.release`` directly, so the release is bounded and lease-return
    marked like every other kind).

    Steps, in order:

    1. LIVENESS (before any execution): a missing row releases the lease only
       (no entity write possible); an owning session that is gone, ENDED or
       cancel-requested, or a live park that no longer references the task's id
       (the orphan, D-A2-06) is a VOID: the row is failed terminally WITHOUT
       running the tool (the lease drops, it is never QUEUED again). A GATED
       row is left alone (entity-only hand-back; the gate is another slice's
       state).
    2. MINT THE TOKEN AND CLAIM THE ROW: one fenced ``patch_if`` writes
       ``state=running``, ``started_at`` and the fresh claim token (the
       takeover overwrites the dead previous holder's token by design and
       counts the unclean execution on the row's ``attempts``); a refused patch
       (the row moved) re-reads and branches on what it became.
    3. THE RETRY CAP (D-A2-03): a lease that has already failed
       ``max_attempts`` times does not run the tool again: it goes terminal
       FAILED carrying a SYNTHESIZED error result_state, so the resume
       coordinator hands the model a tool_result for its tool_use instead of
       the turn wedging on an unbounded requeue.
    4. ROUTE RESOLVED GATES FIRST: a resolved occurrence is fresh-read and
       fenced against its RUNNING row, claim token, live session park,
       binding epoch and full canonical envelope before any ordinary-call
       dependency is touched. Generic yields and refused approvals consume
       without a workspace or manager; approved approvals build only the
       authorized manager they need, without constructing a full executor.
       A resolved setup or hook failure becomes one
       terminal, credential-redacted result and never a transient requeue.
    5. REHYDRATE AN ORDINARY CALL: the task row is ref-only; the durable
       TOOL_CALL record the ``record_seq`` points at carries the name and
       arguments (``read_tool_call_record`` fails loudly on any invariant
       break, which terminates the task instead of requeueing a known-bad
       row).
    6. EXECUTE an ordinary call through the session's own executor build (the same
       ``build_agent_executor`` the turn path uses, so the tool surface, the
       approval gate and the run's principal are the turn's own), mapping the
       outcome exactly one way each: success (a tool-level error is still a
       successful execution and stays DONE with the result), gate (the
       approval/yield YieldToWorker parks the TASK, not the session), and
       transient failure (requeue with the config's backoff).
    7. Heartbeat and cancel ride the pool's generic loops (the pool's
       ``_release_lease`` marks the scope lease-returned; a lost lease preempts
       the execution). A cancelled execution re-raises: the row keeps the
       token and the next claim takes it over (at-least-once, step 8).
    8. IDEMPOTENCY: the executor writes ONLY ``result_state`` on the row
       (through the fenced release); the durable TOOL_RESULT transcript
       records are written exactly once by the resume coordinator under the
       session log's single-writer lock, so the transcript cannot duplicate
       while the tool's own external side effects may (at-least-once).
    """
    from primer.claim.tool_call_lookup import (
        ToolCallRecordMismatch,
        read_tool_call_record,
    )
    from primer.int.claim import ParkRequest, ReleaseOutcome
    from primer.model.chat import ToolCallPart, ToolResultPart
    from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
    from primer.model.yield_ import YieldToWorker
    from primer.model.workspace_session import WorkspaceSession
    from primer.session.tool_wait_batches import task_is_live_for_session
    from primer.worker.task_gate_runtime import (
        TaskGateDecisionClass,
        TaskGateState,
        TaskGateStateError,
        classify_task_gate_decision,
        decode_task_gate_state,
        new_task_gate_state,
    )

    if pool._storage is None:
        # A pool built without storage (bare loop tests) cannot read the row,
        # the session or the durable record: hand the lease back, entity-only.
        logger.error(
            "tool-call claim of %s has no storage provider: the lease is "
            "handed back without running",
            engine_lease.entity_id,
        )
        await pool._release_lease(
            engine_lease, ReleaseOutcome(success=True, drop_lease=True, entity_noop=True),
        )
        return

    task_storage = pool._storage.get_storage(ToolCallTask)
    session_storage = pool._storage.get_storage(WorkspaceSession)

    # ---- 1. liveness ----------------------------------------------------------
    task = await task_storage.get(engine_lease.entity_id)
    if task is None:
        # The row is gone: release the lease only (no entity write is possible).
        logger.warning(
            "tool-call claim of %s: no task row: the lease is dropped, nothing runs",
            engine_lease.entity_id,
        )
        await pool._release_lease(
            engine_lease, ReleaseOutcome(success=True, drop_lease=True, entity_noop=True),
        )
        return

    if task.state is ToolCallTaskState.GATED:
        # Defensive: a gated row is not claimable in normal operation (the
        # eligibility filter excludes it) and its gate is the decisions slice's
        # state. Leave the gate alone; the hand-back is lease-only.
        await pool._release_lease(
            engine_lease,
            ReleaseOutcome(
                success=True, entity_noop=True,
                requeue_after=timedelta(seconds=5.0),
            ),
        )
        return

    resolved_gate = None
    gate_error = None
    if task.gate_state is not None or task.gate_resolution is not None:
        try:
            decoded_gate = decode_task_gate_state(task.gate_state, task=task)
            decision_class = (
                classify_task_gate_decision(task, decoded_gate.resolution)
                if isinstance(decoded_gate, TaskGateState)
                and decoded_gate.resolution is not None
                else None
            )
            if (
                not isinstance(decoded_gate, TaskGateState)
                or decoded_gate.resolution is None
                or not (
                    decision_class is TaskGateDecisionClass.IDENTICAL
                    or (
                        task.state is ToolCallTaskState.RUNNING
                        and decision_class is TaskGateDecisionClass.RUNNING
                    )
                )
            ):
                raise TaskGateStateError(
                    "task does not carry one exact resumable v1 gate"
                )
            resolved_gate = decoded_gate
        except TaskGateStateError as exc:
            gate_error = exc

    session = await session_storage.get(task.session_id)
    live, void_reason = task_is_live_for_session(task, session)
    if not live:
        await _void_tool_call_task(
            pool, engine_lease, task_storage, task, void_reason,
        )
        return

    # ---- 2. mint the token and claim the row -----------------------------------
    token, claimed_row = await _claim_tool_call_row(
        pool, task_storage, task, engine_lease,
    )
    if token is None:
        # The row moved under the step-1 read (a stale release from the previous
        # holder landed first): branch on what it became.
        fresh = await task_storage.get(task.id)
        if fresh is None or fresh.state in (
            ToolCallTaskState.DONE, ToolCallTaskState.FAILED,
        ):
            await pool._release_lease(
                engine_lease,
                ReleaseOutcome(success=True, drop_lease=True, entity_noop=True),
            )
        else:
            await pool._release_lease(
                engine_lease,
                ReleaseOutcome(
                    success=True, entity_noop=True, requeue_after=timedelta(seconds=5.0),
                ),
            )
        return

    # ---- 3. the retry cap (D-A2-03) ---------------------------------------------
    # The cap counts UNCLEAN executions across holders: the lease's
    # attempt_count (bumped by every release of it) and the row's attempts
    # (bumped by every takeover of a crashed holder's RUNNING row - a crash
    # the lease never sees, because reclaiming an expired lease starts at 0).
    # GR-7: read the two together, so a tool whose execution kills its worker
    # is failed at the cap instead of taken over and re-run for ever.
    if max(engine_lease.attempt_count, claimed_row.attempts) >= pool.config.max_attempts:
        # The cap: the tool has already failed max_attempts times; it does not
        # run again. Terminal FAILED WITH a synthesized result_state, so the
        # resume coordinator (which reads result_state first) hands the model a
        # tool_result for its tool_use instead of wedging the turn.
        last_error = engine_lease.last_error or task.last_error or "tool call failed"
        logger.warning(
            "tool-call task %s reached the retry cap (lease attempt_count=%d, "
            "row attempts=%d >= max_attempts=%d): failing it terminally without running the tool",
            task.id, engine_lease.attempt_count, claimed_row.attempts, pool.config.max_attempts,
        )
        await pool._release_lease(
            engine_lease,
            ReleaseOutcome(
                success=False,
                drop_lease=True,
                claim_token=token,
                last_error=last_error[:300],
                entity_update={
                    "result_state": {
                        "id": task.wire_call_id,
                        "output": last_error[:300],
                        "error": True,
                    },
                    "attempts": claimed_row.attempts,
                    "last_error": last_error[:300],
                },
            ),
        )
        return

    if gate_error is not None:
        await _fail_claimed_tool_call_task(
            pool, engine_lease, token, claimed_row,
            f"resolved task gate is corrupt: {_short_error(gate_error)}",
        )
        return

    if resolved_gate is not None:
        # A resolved occurrence is already its own durable source of truth.
        # Fence it against the freshly claimed row and live session before
        # touching any ordinary-call dependency (workspace, transcript,
        # executor or tool manager). Generic yields and rejected approvals do
        # not need those dependencies at all; an approved approval builds only
        # its narrow authorized manager after the fence holds.
        await _run_resolved_task_gate(
            pool,
            engine_lease,
            task_storage=task_storage,
            session_storage=session_storage,
            claimed_row=claimed_row,
            token=token,
            gate=resolved_gate,
        )
        return

    # ---- 4. rehydrate the call ---------------------------------------------------
    workspace = None
    if pool._workspace_registry is not None:
        workspace = await pool._workspace_registry.get_workspace(session.workspace_id)
    if workspace is None:
        await _transient_tool_call_failure(
            pool, engine_lease, token, claimed_row.attempts,
            RuntimeError(
                f"workspace {session.workspace_id!r} could not be resolved; "
                "the task's durable record cannot be read"
            ),
        )
        return

    try:
        record = await read_tool_call_record(_WorkspaceReadFileIO(workspace), task)
    except ToolCallRecordMismatch as exc:
        # The "record durable before task claimable" ordering invariant broke
        # upstream: fail loudly, terminal, no requeue (a known-bad row must not
        # burn the whole retry budget).
        logger.error(
            "tool-call task %s: its own TOOL_CALL record is missing or "
            "mismatched: %s",
            task.id, exc,
        )
        await pool._release_lease(
            engine_lease,
            ReleaseOutcome(
                success=False,
                drop_lease=True,
                claim_token=token,
                last_error=_short_error(exc),
                entity_update={
                    "attempts": claimed_row.attempts,
                    "last_error": _short_error(exc),
                },
            ),
        )
        return

    call = ToolCallPart(
        id=task.wire_call_id,
        name=record.payload["name"],
        arguments=record.payload.get("arguments") or {},
    )

    # ---- 5. execute through the session's own tool surface ------------------------
    # The turn path's own seam: the executor build resolves the agent, the LLM
    # and the toolsets, and stamps the run's principal (resolve_run_principal);
    # the manager's initiated_by carries the authority, so execute is called
    # the same way the loop calls it (principal=None).
    try:
        executor = await pool._build_agent_executor(session, workspace)
    except Exception as exc:  # noqa: BLE001 - the build is one failure class here (RunRefused included)
        await _transient_tool_call_failure(pool, engine_lease, token, claimed_row.attempts, exc)
        return
    base = getattr(executor, "_executor", executor)
    tool_manager = getattr(base, "_tool_manager", None)
    if tool_manager is None:
        await _transient_tool_call_failure(
            pool, engine_lease, token, claimed_row.attempts,
            RuntimeError("the session executor carries no tool manager"),
        )
        return


    try:
        result = await tool_manager.execute(call, principal=None)
    except asyncio.CancelledError:
        # A lost lease (or a drain) preempted the execution: the pool owns the
        # lease from here. Re-raise so the pool's scope machinery runs; the row
        # keeps the token and the next claim takes it over (at-least-once).
        raise
    except YieldToWorker as yld:
        # The approval/yield gate: park the TASK, not the session. The adapter's
        # gate branch writes GATED, bumps gate_seq, clears the token and drops
        # the lease; the session is NOT woken (one gated task does not block
        # its siblings). The decision side (respond -> flip) is the separate
        # decisions slice (D-A2-09).
        y = yld.yielded
        now = _now()
        timeout = (
            y.timeout if y.timeout is not None
            else _TASK_GATE_DEFAULT_TIMEOUT_SECONDS
        )
        deadline = now + timedelta(seconds=timeout)
        try:
            gate_state = new_task_gate_state(
                yld,
                task=claimed_row,
                gate_seq=claimed_row.gate_seq + 1,
                parked_at=now,
                deadline=deadline,
                binding_epoch=session.binding_epoch,
                tool_arguments=call.arguments,
            )
        except TaskGateStateError as exc:
            # Nested/corrupt topology cannot be resumed safely at task scope.
            # Fail this task terminally instead of dropping its lease with an
            # envelope no later worker can consume.
            reason = _short_error(exc)
            logger.error(
                "tool-call task %s produced an unsupported gate: %s",
                task.id,
                reason,
            )
            await pool._release_lease(
                engine_lease,
                ReleaseOutcome(
                    success=False,
                    drop_lease=True,
                    claim_token=token,
                    last_error=reason,
                    entity_update={
                        "result_state": {
                            "id": task.wire_call_id,
                            "output": reason,
                            "error": True,
                        },
                        "attempts": claimed_row.attempts,
                        "last_error": reason,
                    },
                ),
            )
            return
        logger.info(
            "tool-call task %s gated by %r (event_key=%r): the task parks, "
            "the session's park is untouched",
            task.id, y.tool_name, y.event_key,
        )
        await pool._release_lease(
            engine_lease,
            ReleaseOutcome(
                success=True,
                drop_lease=True,
                claim_token=token,
                park=ParkRequest(
                    parked_state=gate_state.to_jsonable(),
                    parked_event_key=y.event_key,
                    parked_until=deadline,
                    parked_at=now,
                ),
            ),
        )
        return
    except Exception as exc:  # noqa: BLE001 - ordinary setup/execution retries
        await _transient_tool_call_failure(
            pool, engine_lease, token, claimed_row.attempts, exc,
        )
        return

    # A tool-level error (result.error) is still a SUCCESSFUL execution: the
    # row stays DONE carrying the real result_state, so the resume coordinator
    # shows the model the tool's own error under its own tool_use.
    await pool._release_lease(
        engine_lease,
        ReleaseOutcome(
            success=True,
            drop_lease=True,
            claim_token=token,
            entity_update={
                "result_state": result.model_dump(mode="json"),
                "attempts": claimed_row.attempts,
                "last_error": result.output[:300] if result.error else None,
            },
        ),
    )


async def _run_resolved_task_gate(
    pool,
    engine_lease,
    *,
    task_storage,
    session_storage,
    claimed_row,
    token: str,
    gate,
) -> None:
    """Fence and consume one resolved occurrence before ordinary-call setup."""
    from primer.int.claim import ParkRequest, ReleaseOutcome
    from primer.model.tool_call_task import ToolCallTaskState
    from primer.model.yield_ import YieldToWorker
    from primer.session.tool_wait_batches import (
        batches_referenced_by_park,
        task_is_live_for_session,
    )
    from primer.storage._patch import json_equal
    from primer.worker.task_gate_runtime import (
        TaskGateState,
        TaskGateStateError,
        decode_task_gate_state,
        new_task_gate_state,
    )

    current_task = await task_storage.get(claimed_row.id)
    current_session = await session_storage.get(claimed_row.session_id)
    if current_task is None:
        current_live = False
        current_reason = "the task row disappeared after it was claimed"
    else:
        current_live, current_reason = task_is_live_for_session(
            current_task, current_session,
        )
    park_holds_task = False
    if current_session is not None:
        park_holds_task = any(
            claimed_row.id in (*ref.outstanding_ids, *ref.notifying_ids)
            for ref in batches_referenced_by_park(
                current_session.parked_state,
            ).values()
        )
    if (
        current_task is None
        or current_task.state is not ToolCallTaskState.RUNNING
        or current_task.claim_token != token
        or not current_live
        or not park_holds_task
        or gate.binding_epoch != current_session.binding_epoch
    ):
        reason = current_reason or (
            "the resolved task gate no longer matches the live session binding"
        )
        await _fail_claimed_tool_call_task(
            pool, engine_lease, token, claimed_row, reason,
        )
        return
    try:
        current_gate = decode_task_gate_state(
            current_task.gate_state, task=current_task,
        )
    except TaskGateStateError as exc:
        await _fail_claimed_tool_call_task(
            pool, engine_lease, token, claimed_row,
            f"resolved task gate changed after claim: {_short_error(exc)}",
        )
        return
    if (
        not isinstance(current_gate, TaskGateState)
        or not json_equal(current_gate.to_jsonable(), gate.to_jsonable())
    ):
        await _fail_claimed_tool_call_task(
            pool, engine_lease, token, claimed_row,
            "resolved task gate changed after claim",
        )
        return

    try:
        tool_manager = None
        if _resolved_gate_needs_tool_manager(gate):
            workspace = None
            if pool._workspace_registry is not None:
                workspace = await pool._workspace_registry.get_workspace(
                    current_session.workspace_id,
                )
            if workspace is None:
                raise RuntimeError(
                    f"workspace {current_session.workspace_id!r} could not be resolved for approved task-gate resume"
                )
            tool_manager = await pool._build_agent_tool_manager(
                current_session,
                workspace,
                required_tool_name=current_task.tool_name,
            )
        result = await _consume_resolved_task_gate(
            pool, gate, current_session, tool_manager,
        )
    except asyncio.CancelledError:
        # A lost lease or drain leaves the RUNNING row, its claim token and
        # exact receipt intact. A later takeover re-fences and re-consumes it.
        raise
    except YieldToWorker as yld:
        y = yld.yielded
        now = _now()
        timeout = (
            y.timeout if y.timeout is not None
            else _TASK_GATE_DEFAULT_TIMEOUT_SECONDS
        )
        deadline = now + timedelta(seconds=timeout)
        try:
            next_gate = new_task_gate_state(
                yld,
                task=claimed_row,
                gate_seq=claimed_row.gate_seq + 1,
                parked_at=now,
                deadline=deadline,
                binding_epoch=current_session.binding_epoch,
                tool_arguments=gate.tool_arguments,
            )
        except TaskGateStateError as exc:
            reason = _short_error(exc)
            logger.error(
                "tool-call task %s produced an unsupported resumed gate: %s",
                claimed_row.id,
                reason,
            )
            await pool._release_lease(
                engine_lease,
                ReleaseOutcome(
                    success=False,
                    drop_lease=True,
                    claim_token=token,
                    last_error=reason,
                    entity_update={
                        "result_state": {
                            "id": claimed_row.wire_call_id,
                            "output": reason,
                            "error": True,
                        },
                        "attempts": claimed_row.attempts,
                        "last_error": reason,
                    },
                ),
            )
            return
        await pool._release_lease(
            engine_lease,
            ReleaseOutcome(
                success=True,
                drop_lease=True,
                claim_token=token,
                park=ParkRequest(
                    parked_state=next_gate.to_jsonable(),
                    parked_event_key=y.event_key,
                    parked_until=deadline,
                    parked_at=now,
                ),
            ),
        )
        return
    except Exception as exc:  # noqa: BLE001 - resolved resumes fail terminally
        logger.exception(
            "resolved task gate for %s raised; synthesising an error result",
            claimed_row.id,
        )
        from primer.worker.yield_runtime import resume_failure_result

        result = resume_failure_result(
            tool_call_id=claimed_row.wire_call_id,
            tool_name=gate.yielded.tool_name,
            exc=exc,
        )

    await pool._release_lease(
        engine_lease,
        ReleaseOutcome(
            success=True,
            drop_lease=True,
            claim_token=token,
            entity_update={
                "result_state": result.model_dump(mode="json"),
                "attempts": claimed_row.attempts,
                "last_error": result.output[:300] if result.error else None,
            },
        ),
    )


def _resolved_gate_needs_tool_manager(gate) -> bool:
    """Whether consuming ``gate`` can dispatch its approved original call."""
    if gate.yielded.tool_name != "_approval":
        return False
    from primer.worker.yield_runtime import (
        classify_approval_payload,
        classify_marker_payload,
    )

    resume = classify_marker_payload(
        gate.resolution.payload, parked_at=gate.parked_at,
    )
    decision, _reason, _kind = classify_approval_payload(resume.payload)
    return decision == "approved"


async def _consume_resolved_task_gate(pool, gate, session, tool_manager):
    """Consume one already-validated v1 receipt through shared resume semantics."""
    from primer.model.chat import ToolResultPart
    from primer.worker.yield_resume_registry import ResumeContext, get_resume_hook
    from primer.worker.yield_runtime import (
        _resume_tool_approval,
        classify_approval_payload,
        classify_marker_payload,
        stamp_parked_at,
    )

    receipt = gate.resolution
    if receipt is None:  # defensive; the caller validated this already
        raise ValueError("resolved task gate has no resolution receipt")
    resume = classify_marker_payload(
        receipt.payload, parked_at=gate.parked_at,
    )
    if gate.yielded.tool_name == "_approval":
        from primer.agent.approval_record import (
            gate_record_key,
            record_from_parked_blob,
            write_approval_record,
        )
        from primer.model.tool_approval import ToolApprovalRecord

        decision, reason, _kind = classify_approval_payload(resume.payload)
        record = record_from_parked_blob(
            blob=gate.to_jsonable(),
            decision=decision,
            reason=reason,
            agent_id=getattr(session.binding, "agent_id", None),
            session_id=session.id,
            workspace_id=session.workspace_id,
            requested_at=gate.parked_at,
            decided_by=(
                resume.payload.get("decided_by")
                if isinstance(resume.payload, dict) else None
            ),
            gate_event_key=gate.event_key,
        ).model_copy(update={
            "gate_event_key": gate_record_key(
                gate.event_key, gate.occurrence_token,
            ),
        })
        await write_approval_record(
            pool._storage.get_storage(ToolApprovalRecord),
            record,
            warn_on_decision_mismatch=True,
        )
        return await _resume_tool_approval(
            blob=gate.to_jsonable(),
            payload=resume.payload,
            tool_manager=tool_manager,
        )

    hook = get_resume_hook(gate.yielded.tool_name)
    resume_metadata = stamp_parked_at(
        gate.yielded, gate.parked_at,
    ).resume_metadata
    registry = getattr(pool, "_provider_registry", None)
    hook_result = hook(
        resume_metadata,
        resume.payload,
        ResumeContext(
            tool_name=gate.yielded.tool_name,
            tool_call_id=gate.tool_call_id,
            session_id=session.id,
            resolve_provider=(
                registry.get_toolset if registry is not None else None
            ),
        ),
    )
    if asyncio.iscoroutine(hook_result):
        hook_result = await hook_result
    return ToolResultPart(
        id=gate.tool_call_id,
        output=hook_result.output,
        error=hook_result.is_error,
    )


async def _fail_claimed_tool_call_task(
    pool, engine_lease, token: str, row, reason: str,
) -> None:
    """Fail one claimed task terminally without clearing its gate early."""
    from primer.int.claim import ReleaseOutcome

    reason = reason[:300]
    await pool._release_lease(
        engine_lease,
        ReleaseOutcome(
            success=False,
            drop_lease=True,
            claim_token=token,
            last_error=reason,
            entity_update={
                "result_state": {
                    "id": row.wire_call_id,
                    "output": reason,
                    "error": True,
                },
                "attempts": row.attempts,
                "last_error": reason,
            },
        ),
    )


async def _void_tool_call_task(
    pool: "WorkerPool",
    engine_lease: "ClaimLease",
    task_storage,
    task,
    reason: str,
) -> None:
    """The step-1 void: terminal FAILED WITHOUT running the tool, lease dropped.

    The adapter's terminal branch fences on the claim token, so the void still
    goes through the step-2 claim (mint + fenced RUNNING patch) first: the token
    must be on the row before the terminal write can match its fence. A refused
    claim (the row moved) branches on what it became instead of forcing a write.
    No result_state is written: the owning session is gone, ended or parked
    elsewhere, so the resume coordinator never reads this row; last_error is the
    queryable summary (and what a stale reader is shown).
    """
    from primer.int.claim import ReleaseOutcome
    from primer.model.tool_call_task import ToolCallTaskState

    token, claimed_row = await _claim_tool_call_row(
        pool, task_storage, task, engine_lease,
    )
    if token is None:
        fresh = await task_storage.get(task.id)
        if fresh is None or fresh.state in (
            ToolCallTaskState.DONE, ToolCallTaskState.FAILED,
        ):
            await pool._release_lease(
                engine_lease,
                ReleaseOutcome(success=True, drop_lease=True, entity_noop=True),
            )
        else:
            await pool._release_lease(
                engine_lease,
                ReleaseOutcome(
                    success=True, entity_noop=True, requeue_after=timedelta(seconds=5.0),
                ),
            )
        return
    logger.info(
        "tool-call task %s voided without running (%s)", task.id, reason,
    )
    await pool._release_lease(
        engine_lease,
        ReleaseOutcome(
            success=False,
            drop_lease=True,
            claim_token=token,
            last_error=reason[:300],
            entity_update={
                "attempts": claimed_row.attempts,
                "last_error": reason[:300],
            },
        ),
    )


async def _claim_tool_call_row(
    pool: "WorkerPool",
    task_storage,
    task,
    engine_lease: "ClaimLease",
) -> "tuple[str | None, Any]":
    """Step 2: one fenced patch claiming the row under a freshly minted token.

    Returns ``(token, the row as the patch wrote it)``; ``(None, None)`` when
    the row moved under every attempt (a terminal or gated row the caller must
    re-read and branch on).

    The fence names the state band [queued, running] AND the read ``attempts``
    value, so a concurrent increment (a stale failed release from the previous
    holder landing between the read and the write) refuses the patch instead of
    being lost - the read-then-write the row's own contract demands. The
    takeover (the row was RUNNING: the previous holder died mid-run) counts the
    unclean execution here, on the way into RUNNING, so every branch that
    follows sees the advanced count.
    """
    from pydantic_core import to_jsonable_python
    from primer.model.tool_call_task import ToolCallTaskState
    import secrets

    last = task
    for _ in range(_CLAIM_ROW_MAX_ATTEMPTS):
        token = (
            f"{pool._worker_id}:{engine_lease.claimed_at.isoformat()}:"
            f"{secrets.token_hex(6)}"
        )
        took_over = last.state is ToolCallTaskState.RUNNING
        patch = {
            "state": ToolCallTaskState.RUNNING.value,
            "started_at": datetime.now(timezone.utc),
            "claim_token": token,
        }
        if took_over:
            patch["attempts"] = last.attempts + 1
        where = {
            "state": [
                ToolCallTaskState.QUEUED.value, ToolCallTaskState.RUNNING.value,
            ],
            "attempts": [last.attempts],
        }
        # JSON-ready, like the adapter's own pre-encoding: patch_if validates
        # the values against the storage backends' JSON document contract.
        updated = await task_storage.patch_if(
            task.id, to_jsonable_python(patch), where=where,
        )
        if updated is not None:
            return token, updated
        fresh = await task_storage.get(task.id)
        if fresh is None or fresh.state in (
            ToolCallTaskState.DONE, ToolCallTaskState.FAILED,
            ToolCallTaskState.GATED,
        ):
            return None, None
        last = fresh
    logger.warning(
        "tool-call task %s: the claim patch was refused %d times: the row is "
        "left as-is and the lease is handed back",
        task.id, _CLAIM_ROW_MAX_ATTEMPTS,
    )
    return None, None


async def _transient_tool_call_failure(
    pool: "WorkerPool",
    engine_lease: "ClaimLease",
    token: str,
    row_attempts: int,
    exc: BaseException,
) -> None:
    """The 5c branch: requeue the lease with the config's backoff; the adapter
    resets the row to QUEUED, clears the token and result_state, and the engine
    bumps the lease's attempt_count. The next claim (this pool or a peer) takes
    it up; the cap (step 3) ends the requeueing."""
    from primer.int.claim import ReleaseOutcome

    failures = engine_lease.attempt_count + 1
    backoff = _tool_call_backoff_seconds(pool.config, failures)
    last_error = _short_error(exc)
    logger.warning(
        "tool-call task %s failed transiently (attempt %d, requeue in %.2fs): %s",
        engine_lease.entity_id, failures, backoff, last_error,
    )
    await pool._release_lease(
        engine_lease,
        ReleaseOutcome(
            success=False,
            drop_lease=False,
            claim_token=token,
            requeue_after=timedelta(seconds=backoff),
            last_error=last_error,
            entity_update={
                "attempts": row_attempts + 1,
                "last_error": last_error,
            },
        ),
    )
