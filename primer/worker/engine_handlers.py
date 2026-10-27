"""Per-kind engine claim handlers for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). One
function per non-session claim kind (harness / trigger); each takes the
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
module (and ``pool``) doesn't pull the harness / trigger dependency
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


async def _lease_after_the_fire(pool: "WorkerPool", trigger_id: str) -> tuple[bool, timedelta | None]:
    """How a claim that fired gives its lease back: ``(drop_lease, requeue_after)`` (ticket 01a16ddb, review of #915).

    The lease is requeued to the trigger's next tick, the one the release records (``TriggerClaimAdapter.on_release``: the next cron tick
    after now for a ``scheduled`` trigger), so it is not claimable again before it. Without the requeue the in-memory claim engine, which did
    not evaluate the adapter's eligibility then, re-claimed a released trigger at once, and every scheduled trigger re-fired in a loop on a
    SQLite deployment; it now applies the trigger's eligibility too (ticket 01a16e79), and the requeue keeps the lease out of each claim's
    entity reads until its tick. A trigger with no next tick (a ``delayed`` trigger that fired, a disabled or deleted one, an unreadable cron) drops the
    lease: the save that lets it fire again upserts a new one (``primer.trigger.service._upsert_claim_if_eligible``). A row that cannot be
    read leaves the lease as the engine would.
    """
    from primer.model.trigger import Trigger
    from primer.trigger.cron import next_fire_at

    try:
        trigger = await pool._storage.get_storage(Trigger).get(trigger_id)
    except Exception:
        logger.exception("trigger %s: could not read it to requeue its lease", trigger_id)
        return False, None
    if trigger is None or not trigger.enabled or trigger.config.kind != "scheduled":
        return True, None
    now = _now()
    try:
        upcoming = next_fire_at(trigger.config.cron, trigger.config.timezone, after=now)
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
            drop, requeue_after = await _lease_after_the_fire(pool, engine_lease.entity_id)
            outcome = ReleaseOutcome(
                success=success, drop_lease=drop, requeue_after=requeue_after, last_error=last_error,
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

    ``read_record_by_seq`` builds the messages.jsonl path from the ``state_path``
    attribute and reads through ``read_file``; both delegate to the workspace
    backend, the same seam the pool's own write-side shim (``_WorkspaceIOShim.
    read_state_file``) uses, so a workspace that overrides its state path via a
    template is honoured on both sides.
    """

    def __init__(self, workspace) -> None:
        self._workspace = workspace
        self.state_path = getattr(workspace, "state_path", ".state")

    async def read_file(self, path: str) -> bytes:
        return await self._workspace.read_file(path)


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
    4. REHYDRATE THE CALL: the task row is ref-only; the durable TOOL_CALL
       record the ``record_seq`` points at carries the name and arguments
       (``read_tool_call_record`` fails loudly on any invariant break, which
       terminates the task instead of requeueing a known-bad row).
    5. EXECUTE through the session's own executor build (the same
       ``build_agent_executor`` the turn path uses, so the tool surface, the
       approval gate and the run's principal are the turn's own), mapping the
       outcome exactly one way each: success (a tool-level error is still a
       successful execution and stays DONE with the result), gate (the
       approval/yield YieldToWorker parks the TASK, not the session), and
       transient failure (requeue with the config's backoff).
    6-7. Heartbeat and cancel ride the pool's generic loops (the pool's
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
    from primer.model.chat import ToolCallPart
    from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
    from primer.model.yield_ import YieldToWorker
    from primer.model.workspace_session import WorkspaceSession
    from primer.session.tool_wait_batches import task_is_live_for_session

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
    if engine_lease.attempt_count >= pool.config.max_attempts:
        # The cap: the tool has already failed max_attempts times; it does not
        # run again. Terminal FAILED WITH a synthesized result_state, so the
        # resume coordinator (which reads result_state first) hands the model a
        # tool_result for its tool_use instead of wedging the turn.
        last_error = engine_lease.last_error or task.last_error or "tool call failed"
        logger.warning(
            "tool-call task %s reached the retry cap (attempt_count=%d >= "
            "max_attempts=%d): failing it terminally without running the tool",
            task.id, engine_lease.attempt_count, pool.config.max_attempts,
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
                    parked_state=y.resume_metadata,
                    parked_event_key=y.event_key,
                    parked_until=now + timedelta(seconds=timeout),
                    parked_at=now,
                ),
            ),
        )
        return
    except Exception as exc:  # noqa: BLE001 - every non-gate failure is transient here
        await _transient_tool_call_failure(pool, engine_lease, token, claimed_row.attempts, exc)
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
