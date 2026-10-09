"""Background worker pool — claims sessions and harnesses and runs one turn each.

Claim loop architecture:
* A single ``_engine_claim_loop`` and ``_engine_bus_loop`` handle all claim
  kinds (session, harness) via the injected ``ClaimEngine``.

One unified ``_in_flight: set[tuple[ClaimKind, str]]`` tracks all in-flight
items regardless of kind.  Capacity: ``free = max_concurrency - len(_in_flight)``.

See ``docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md``
§6 for the full design.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import sys
import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import asyncpg

from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.int.claim import Lease as ClaimLease
from primer.int.scheduler import (
    Scheduler,
)
from primer.model.except_ import ListenConnectionLost
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.model.workspace_session import WorkspaceSession, SessionStatus
from primer.model.yield_ import CANCEL_REASON_PREEMPTED
from primer.worker.turn import _CancelScope
from primer.worker.drivers import _GraphTurnDriver, _TurnDriver  # noqa: F401  re-export
from primer.worker.io_shim import _WorkspaceIOShim
from primer.worker.identity import stable_worker_label
from primer.worker._toolset_ids import _toolset_ids_from_scoped  # noqa: F401  re-export

import primer.observability.metrics as _metrics
from primer.session.dispatch import (
    SessionDispatchDeps,
    clear_interrupt_for_resume,
    pause_session_for_refused_workspace,
    run_one_session_turn,
)

if TYPE_CHECKING:
    from primer.agent.approval import ApprovalResolver
    from primer.api.registries import ProviderRegistry, WorkspaceRegistry
    from primer.graph.router import RouterRegistry
    from primer.int.claim import ClaimEngine
    from primer.int.event_bus import EventBus
    from primer.int.storage_provider import StorageProvider

logger = logging.getLogger(__name__)

# Restart wait for _engine_bus_loop: starts at the initial value, doubles while
# the watcher keeps failing, and is capped. A watcher that stayed up at least as
# long as the cap is healthy again, so its failure is a fresh incident and the
# wait starts over from the initial value.
_ENGINE_BUS_BACKOFF_INITIAL_S = 1.0
_ENGINE_BUS_BACKOFF_MAX_S = 30.0

# What connecting or re-subscribing raises while Postgres is down, restarting or
# shutting down. Each is expected during an outage, so it is a one-line WARNING,
# not an ERROR with a traceback.
_OUTAGE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,                           # refused, reset, timed out, unresolvable host
    asyncpg.PostgresConnectionError,   # SQLSTATE 08xxx, e.g. ConnectionDoesNotExistError mid-LISTEN
    asyncpg.CannotConnectNowError,     # 57P03: the server is starting up or shutting down
    asyncpg.AdminShutdownError,        # 57P01: terminated by a shutdown or pg_terminate_backend
    asyncpg.CrashShutdownError,        # 57P02: a crash shutdown terminated the connection
)


class WorkerPool:
    """Per-process worker pool: claims sessions and runs one turn each."""

    def __init__(
        self,
        *,
        config: WorkerConfig,
        scheduler: Scheduler,
        storage: "StorageProvider",
        workspace_registry: "WorkspaceRegistry",
        provider_registry: "ProviderRegistry",
        semantic_search_registry: Any | None = None,
        router_registry: "RouterRegistry | None" = None,
        approval_resolver: "ApprovalResolver | None" = None,
        channel_dispatcher=None,
        event_bus: "EventBus | None" = None,
        artifact_storage_registry: Any | None = None,
        engine: "ClaimEngine",
    ) -> None:
        self.config = config
        self._scheduler = scheduler
        self._storage = storage
        self._workspace_registry = workspace_registry
        self._provider_registry = provider_registry
        # Optional SemanticSearchRegistry so harness-installed documents can
        # be routed through the chunk/embed/index pipeline. None in
        # pure-storage tests (indexing is then skipped, best-effort).
        self._semantic_search_registry = semantic_search_registry
        # Optional RouterRegistry for callable-router edges in graph
        # dispatch. None means only _StaticEdge + _JsonPathRouter edges
        # work; _CallableRouter edges will raise at runtime.
        self._router_registry = router_registry
        self._approval_resolver = approval_resolver
        self._channel_dispatcher = channel_dispatcher
        self._event_bus = event_bus
        self._artifact_storage_registry = artifact_storage_registry
        self._engine = engine

        self._worker_id: str = ""
        # Stable metric label (hostname + index). Computed here, not in
        # start(), so unit tests that never start the pool still label
        # their instrument samples.
        self._worker_label: str = stable_worker_label(config)
        self._tasks: list[asyncio.Task] = []
        self._active_scopes: dict[tuple[ClaimKind, str], _CancelScope] = {}
        # Cancels that reached a running turn through the session row because
        # their NOTIFY never arrived (see _reconcile_cancels). Exposed as
        # primer_worker_cancels_reconciled_total.
        self._cancels_reconciled_total: int = 0
        # Claims of a (kind, id) this pool already has in flight, skipped without
        # touching the lease (see _reserve_and_dispatch). Exposed as
        # primer_worker_duplicate_claims_total; a rate above zero typically means a
        # heartbeat stalled past the lease TTL while a turn was still running.
        self._duplicate_claims_total: int = 0

        # Unified in-flight tracking — one set for all claim kinds.
        # (ClaimKind, entity_id) tuples for all kinds.
        self._in_flight: set[tuple[ClaimKind, str]] = set()

        # Strong references to in-flight per-turn tasks so the GC does not
        # silently collect them between create_task and the first await.
        self._turn_tasks: set[asyncio.Task] = set()
        # Just-claimed leases handed back unstarted because shutdown had begun (see
        # _reserve_and_dispatch). Strong references, awaited by drain_and_stop, so a
        # rolling deploy does not exit with a lease still claimed by a worker that is gone.
        self._unstarted_releases: set[asyncio.Task] = set()
        self._claims_returned_on_drain_total: int = 0
        self._claim_returns_failed_on_drain_total: int = 0
        # Claim or bus loops the drain abandoned because they did not stop within the grace (see _await_stopped_loop).
        self._loops_abandoned_on_drain_total: int = 0
        # Strong references to those abandoned loops until they end: the drain drops its own and the event loop
        # holds tasks only weakly, so one pending on something only it references would be garbage-collected.
        self._abandoned_loops: set[asyncio.Task] = set()
        # A release that has not finished in this long is abandoned (see ``_release_lease``). A slow release does NOT
        # leave the worker's leases heartbeated meanwhile: on Postgres the heartbeat is ONE ``UPDATE`` over every lease
        # this worker holds, the key being released is among them (it stays in ``_in_flight`` until the release ends),
        # and that statement waits on the row lock the release transaction holds, so the WHOLE heartbeat stalls. Each
        # other lease was last refreshed up to one heartbeat interval before the release began, so it can lapse
        # ``lease_ttl - heartbeat_interval`` into the release: the bound is exactly that. ``WorkerConfig`` enforces
        # ``lease_ttl >= 2 * heartbeat_interval``, so it is at least half a TTL: 3 s at the 5 s minimum TTL (heartbeat
        # 2 s), 20 s at the defaults. That keeps the stall within the TTL for ONE slow release only. Overlapping slow
        # releases chain it: the UPDATE waits on each locked row in turn, so a second release that took its row lock
        # before the stalled UPDATE reached that row (say while the first was running or being abandoned) holds the
        # heartbeat until it ends as well, up to one bound after IT began, and the other leases can still lapse.
        self._release_timeout_seconds: float = float(config.lease_ttl_seconds - config.heartbeat_interval_seconds)
        self._release_timeouts_total: int = 0
        # After a timed-out release, how long the one ``has_lease`` probe that decides its outcome may take: a primary-key
        # lookup that has not answered in a heartbeat interval counts as no answer, and the bound plus the probe stay
        # within one lease TTL.
        self._release_probe_timeout_seconds: float = float(config.heartbeat_interval_seconds)
        # Timed-out releases whose probe found the lease row gone (they evidently committed); a subset of the above. It
        # UNDERCOUNTS: a committed release that keeps its row (``drop_lease=False``) reads as present (see _release_lease).
        self._release_timeouts_committed_total: int = 0
        # How long drain waits for the claim loop to finish the iteration it is in (and for hand-backs).
        self._claim_stop_grace_seconds: float = 5.0
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        # Shutdown stops CLAIMING (``_stopping``), not KEEPING what is already running: the lease heartbeat,
        # lost-lease detection and both cancel loops run until the turns have finished or this deadline passes.
        # ``_stopping`` alone would stop them within one heartbeat interval of the drain starting, while a turn
        # can run for ``drain_timeout_seconds`` (default 120) against a lease TTL of 30 s: its lease would expire,
        # a peer would claim it and run a DUPLICATE execution, and the draining worker would never learn of it.
        self._keepalive_done = asyncio.Event()
        self._keepalive_deadline: float | None = None

        # ---- engine-driven loop tasks ----
        self._engine_claim_task: asyncio.Task | None = None
        self._engine_bus_task: asyncio.Task | None = None

        # Dispatch table: routes engine claims by kind to the appropriate
        # per-turn coroutine.  Populated in start() after _worker_id is set.
        self._dispatch: dict[ClaimKind, Callable[[ClaimLease], Coroutine]] = {}

        # ---- metrics (spec §14) ----
        self._claims_total: int = 0
        self._claims_empty_total: int = 0
        self._turns_total_by_result: dict[str, int] = {}
        self._turn_duration_seconds_total: float = 0.0
        self._turn_duration_count: int = 0

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def worker_label(self) -> str:
        """Stable, bounded metric label. See primer.worker.identity."""
        return self._worker_label

    async def start(self) -> None:
        self._worker_id = f"wrk-{uuid.uuid4().hex[:12]}"
        # Tell the scheduler our lease TTL so its claim/heartbeat SQL
        # uses the right interval. Not all impls expose the setter
        # (the ABC doesn't require it), so guard with a try/except.
        try:
            self._scheduler.lease_ttl_seconds = self.config.lease_ttl_seconds  # type: ignore[attr-defined]
        except AttributeError:
            pass
        # Tell the claim engine our lease TTL too, so its claim/heartbeat SQL
        # (postgres) and lease expiry (in-memory) use the configured interval
        # rather than a hardcoded 60s -- this is what makes the
        # lease_ttl >= 2*heartbeat validator actually govern the engine (arch
        # review A-I1). The ClaimEngine ABC doesn't mandate the attribute, so
        # guard it the same way as the scheduler push above.
        try:
            self._engine.lease_ttl_seconds = self.config.lease_ttl_seconds  # type: ignore[attr-defined]
        except AttributeError:
            pass
        await self._scheduler.register_worker(
            worker_id=self._worker_id,
            host=socket.gethostname(),
            pid=os.getpid(),
            capacity=self.config.concurrency,
        )

        # Build the dispatch table now that _worker_id is known.
        self._dispatch = {
            ClaimKind.SESSION: self._run_engine_session,
            ClaimKind.HARNESS: self._run_engine_harness,
            ClaimKind.TRIGGER: self._run_engine_trigger,
        }

        # Engine path: one claim loop + one bus loop.
        # Heartbeat + cancel loops keep the worker row alive and handle
        # mid-turn session cancellations.  _notify_loop is NOT needed —
        # the engine bus loop provides the equivalent wakeup signal.
        self._tasks = [
            asyncio.create_task(
                self._heartbeat_loop(),
                name=f"scheduler-heartbeat-{self._worker_id}",
            ),
            asyncio.create_task(
                self._cancel_loop(),
                name=f"scheduler-cancel-{self._worker_id}",
            ),
            # Its own task, NOT a step of _heartbeat_loop: the row read can
            # block up to command_timeout, which would delay lease heartbeats
            # past the lease TTL.
            asyncio.create_task(
                self._cancel_reconcile_loop(),
                name=f"cancel-reconcile-{self._worker_id}",
            ),
        ]
        self._engine_claim_task = asyncio.create_task(
            self._select_claim_loop()(),
            name=f"engine-claim-{self._worker_id}",
        )
        self._engine_bus_task = asyncio.create_task(
            self._engine_bus_loop(),
            name=f"engine-bus-{self._worker_id}",
        )

    # What drain waits for besides ``drain_timeout_seconds``: the claim loop's last iteration and the hand-back
    # releases (2 x ``_claim_stop_grace_seconds``), the turn tasks after the cancel, and a margin.
    _KEEPALIVE_EXTRA_SECONDS = 30.0

    def _keepalive_over(self) -> bool:
        """True once drain has finished with the turn tasks, or its absolute deadline has passed.

        The deadline bounds the keep-alive even when a turn task ignores its cancel and drain's own wait
        never returns: a worker must not keep a lease alive for ever on behalf of a task it cannot stop.
        """
        if self._keepalive_done.is_set():
            return True
        return (
            self._keepalive_deadline is not None
            and asyncio.get_event_loop().time() >= self._keepalive_deadline
        )

    async def drain_and_stop(self, timeout: float | None = None) -> None:
        drain_timeout = (
            timeout
            if timeout is not None
            else float(self.config.drain_timeout_seconds)
        )
        # ONE clock for the drain: every deadline below is measured from here.
        drain_started = asyncio.get_event_loop().time()
        self._keepalive_deadline = drain_started + drain_timeout + self._KEEPALIVE_EXTRA_SECONDS
        self._stopping.set()
        # Wake any sleeping claim loops so they see the stopping flag.
        self._wake.set()
        # Stop CLAIMING first, before anything else waits: a claim that is already inside
        # claim_due returns its leases after this point, and _reserve_and_dispatch hands them
        # back unstarted instead of dispatching them. The claim task is given a moment to finish
        # that iteration (its loop exits on _stopping) and is only cancelled if it is stuck, since
        # cancelling a claim_due mid-flight can leave rows claimed here with nothing running them
        # (they expire after one lease TTL).
        await self._stop_claiming()
        try:
            await self._scheduler.drain_worker(self._worker_id)
        except Exception:
            logger.exception("drain_worker failed for %s", self._worker_id)
        # The turn wait normally lasts ``drain_timeout`` from here, but it is capped on the drain's own clock: a slow
        # ``drain_worker`` (a database call) must not push it past what the keep-alive deadline covers, or the lease
        # heartbeat could end while turns still run. The cap allows the two ``_stop_claiming`` waits a healthy drain can
        # spend (the claim loop's last iteration, the hand-backs) and no more: a loop that has to be abandoned costs up to
        # two more graces, and they come out of the turn wait.
        deadline = min(
            asyncio.get_event_loop().time() + drain_timeout,
            drain_started + drain_timeout + 2 * self._claim_stop_grace_seconds,
        )
        while self._active_scopes and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
        if self._active_scopes:
            for scope in list(self._active_scopes.values()):
                scope.cancel("worker_drain_timeout")
            self._active_scopes.clear()

        # Wait for all in-flight turn tasks to complete.
        all_tasks_deadline = asyncio.get_event_loop().time() + min(drain_timeout, 5.0)
        while self._turn_tasks and asyncio.get_event_loop().time() < all_tasks_deadline:
            await asyncio.sleep(0.1)
        for task in list(self._turn_tasks):
            task.cancel()
        for task in list(self._turn_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._turn_tasks.clear()

        # Nothing is running any more: only now do the heartbeat and cancel loops stop.
        self._keepalive_done.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        try:
            await self._scheduler.deregister_worker(self._worker_id)
        except Exception:
            logger.exception(
                "deregister_worker failed for %s", self._worker_id,
            )

    async def _stop_claiming(self, grace: float | None = None) -> None:
        """Stop the claim and bus loops, then wait for leases handed back unstarted.

        Every wait here is bounded by ``grace``, including the wait for a CANCELLED loop to finish: a loop stuck in a
        call that ignores cancellation is abandoned (see :meth:`_await_stopped_loop`) so the drain still reaches its
        turn wait. The worst case is therefore four graces (the bus loop's cancel, the claim loop's iteration and then
        its cancel, the hand-backs); ``drain_and_stop`` allows two for it, so a loop that has to be abandoned shortens
        the turn wait rather than pushing it past what the keep-alive covers.
        """
        grace = self._claim_stop_grace_seconds if grace is None else grace
        if self._engine_bus_task is not None:
            self._engine_bus_task.cancel()
            await self._await_stopped_loop(self._engine_bus_task, grace)
            self._engine_bus_task = None
        claim = self._engine_claim_task
        if claim is not None:
            # The loop exits on _stopping at its next check; let an in-flight iteration finish.
            await asyncio.wait({claim}, timeout=grace)
            if not claim.done():
                claim.cancel()
            await self._await_stopped_loop(claim, grace)
            self._engine_claim_task = None
        if self._unstarted_releases:
            # asyncio.wait, not wait_for(gather(...)): a timeout must not cancel a release.
            await asyncio.wait(set(self._unstarted_releases), timeout=grace)

    async def _await_stopped_loop(self, task: asyncio.Task, grace: float) -> None:
        """Wait up to ``grace`` for a loop task that was cancelled (or has stopped) to finish.

        One that does not, because it is inside a call that ignores cancellation, is abandoned: a WARNING names it,
        ``primer_worker_loops_abandoned_on_drain_total`` counts it, ``_abandoned_loops`` keeps it alive until it ends,
        and the drain carries on without it.
        """
        await asyncio.wait({task}, timeout=grace)
        if not task.done():
            self._loops_abandoned_on_drain_total += 1
            self._abandoned_loops.add(task)
            logger.warning(
                "drain: %s did not stop within %.1fs of being cancelled; abandoning it and carrying on with the drain",
                task.get_name(), grace,
            )
        # Retrieve its outcome (now or, for an abandoned loop, whenever it ends) and log an exception it ended with.
        def _retrieve(t: asyncio.Task) -> None:
            self._abandoned_loops.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("drain: %s ended with an exception", t.get_name(), exc_info=t.exception())

        task.add_done_callback(_retrieve)

    async def run_one_turn_now(self, session_id: str) -> None:
        """Test helper: claim and execute exactly one turn for ``session_id``.

        Bypasses the claim loop's polling so tests get a deterministic step
        function. Uses the engine to claim the session. Assumes ``session_id``
        has been upserted into the engine and is ready to claim. Raises if no
        lease is returned (the session wasn't actually runnable).
        """
        from primer.int.claim import ClaimKind as _CK, Lease as _ClaimLease
        engine_leases = await self._engine.claim_due(self._worker_id, max_count=1)
        matching = [l for l in engine_leases if l.entity_id == session_id]
        if not matching:
            raise RuntimeError(
                f"no runnable lease for session {session_id!r}; "
                "did you call engine.upsert first?"
            )
        await self._run_engine_session(matching[0])

    # ---- Metrics ---------------------------------------------------------

    def metrics_snapshot(self) -> dict[str, Any]:
        """Snapshot of worker-pool metrics. See spec §14.

        Synchronous + lock-free: weak consistency is acceptable per
        spec §3 — concurrent claim/complete activity may race the
        snapshot but the values are still useful for dashboards.
        Histograms beyond ``count`` + ``sum`` are deferred (a real
        Prometheus exporter can fold these into proper buckets later)."""
        return {
            "primer_worker_id": self._worker_id,
            "primer_worker_in_flight": len(self._in_flight),
            "primer_worker_capacity": self.config.concurrency,
            "primer_worker_claims_total": self._claims_total,
            "primer_worker_claims_empty_total": self._claims_empty_total,
            "primer_worker_cancels_reconciled_total": self._cancels_reconciled_total,
            "primer_worker_duplicate_claims_total": self._duplicate_claims_total,
            "primer_worker_claims_returned_on_drain_total": self._claims_returned_on_drain_total,
            "primer_worker_claim_returns_failed_on_drain_total": self._claim_returns_failed_on_drain_total,
            "primer_worker_loops_abandoned_on_drain_total": self._loops_abandoned_on_drain_total,
            "primer_worker_release_timeouts_total": self._release_timeouts_total,
            "primer_worker_release_timeouts_committed_total": self._release_timeouts_committed_total,
            "primer_session_turns_total": dict(self._turns_total_by_result),
            "primer_session_turn_duration_seconds": {
                "count": self._turn_duration_count,
                "sum": self._turn_duration_seconds_total,
            },
        }

    # ---- internal --------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        # Keeps running through a drain (see ``_keepalive_done``): the worker row's heartbeat too, because a
        # stale ``last_heartbeat`` is what marks a worker dead and a draining worker is still alive.
        try:
            while not self._keepalive_over():
                await asyncio.sleep(self.config.heartbeat_interval_seconds)
                if self._keepalive_over():
                    return
                try:
                    await self._scheduler.heartbeat_worker(self._worker_id)
                except Exception:
                    logger.exception("heartbeat_loop: scheduler heartbeat failed")
                # The load rides the same tick (Lead sweep M2): the registry is the one place every API sees, and an item count held
                # only in this process was why an API-only /v1/health could not say how busy the fleet was. Advisory and its own
                # try block: a registry that will not take it must not skip the engine heartbeat below.
                try:
                    await self._scheduler.report_worker_load(self._worker_id, in_flight=len(self._in_flight))
                except Exception:
                    logger.exception("heartbeat_loop: reporting this worker's load failed")
                # Two try blocks, not one: the engine heartbeat is what keeps the in-flight leases
                # alive and what preempts a turn whose lease is lost, so a failed scheduler (worker
                # row) heartbeat must not skip it for the whole tick.
                try:
                    # Engine path: heartbeat all in-flight leases via engine.
                    if self._in_flight:
                        # Decide "lost" from what was SENT, never from the live set
                        # after the await: a key dispatched while this round trip is
                        # outstanding was not sent, so it is not in ``confirmed`` and
                        # would be reported lost and cancelled at its start. Capture
                        # the scopes now as well, for the same reason: a key that
                        # finishes and is dispatched again during the round trip owns
                        # a NEW scope, and the cancel must hit the execution whose
                        # lease was actually checked.
                        sent = {
                            kind_id: self._active_scopes.get(kind_id)
                            for kind_id in self._in_flight
                        }
                        confirmed = await self._engine.heartbeat(
                            self._worker_id, list(sent),
                        )
                        confirmed_set = set(confirmed)
                        for kind_id, scope in sent.items():
                            if kind_id in confirmed_set or scope is None:
                                continue
                            if scope.lease_returned:
                                # The execution gave this lease back (see _release_lease), possibly
                                # while this round trip was in flight, so "not confirmed" is what a
                                # release looks like, not a loss. Cancelling now would land in the
                                # post-release tail (the session re-arm) and strand its work.
                                continue
                            # Unconditional on purpose (see _CancelScope): a lease lost
                            # mid-turn must be able to push a turn that is stuck unwinding.
                            scope.cancel(CANCEL_REASON_PREEMPTED)
                except Exception:
                    logger.exception("heartbeat_loop iteration failed")
        except asyncio.CancelledError:
            return

    # ---- engine-driven loops (Task 13: one loop, one bus loop) -----------

    def _select_claim_loop(self) -> Callable[[], Coroutine]:
        """Which claim-loop coroutine function start() should schedule.

        Phase 3 stage 7a (01a0518b) pool-class separation: a reserved TOOL_CALL slice routes to a
        dedicated loop with its own claim_due split (ruling C, leader-approved). Decided once, at
        start - NOT per-iteration - so the unreserved (default) path stays exactly the loop it
        always was, with zero risk of the split logic touching it.

        The condition is literally ``tool_calls_as_claims_enabled and tool_call_reserved_concurrency
        is not None``. ``WorkerConfig`` derives the reserve whenever the flag is on, so "flag and
        reserve" is the whole test. It is deliberately NOT keyed on whether TOOL_CALL is in
        ``_dispatch`` (the earlier guard): from the executor slice on, TOOL_CALL is ALWAYS in
        ``_dispatch`` (the handler is registered unconditionally), so that guard would switch the
        reserved loop on with the flag OFF whenever an explicit reserve is configured, silently
        shrinking general capacity for a slice nothing enqueues into. A reserve set with the flag
        off selects the unreserved loop (pinned in tests/worker/test_pool.py).

        Extracted to its own method (rather than inlined in start()) so this decision is testable
        without fighting start()'s own _dispatch construction.
        """
        if (
            self.config.tool_calls_as_claims_enabled
            and self.config.tool_call_reserved_concurrency is not None
        ):
            return self._engine_claim_loop_reserved
        return self._engine_claim_loop

    async def _engine_claim_loop(self) -> None:
        """Unified claim loop driven by ClaimEngine.claim_due.

        Replaces _claim_loop + _claim_harness_loop when
        an engine is injected. Claims any eligible lease (session or
        harness) and dispatches via self._dispatch[lease.kind].
        """
        assert self._engine is not None
        try:
            while not self._stopping.is_set():
                free = self.config.concurrency - len(self._in_flight)
                if free <= 0:
                    self._wake.clear()
                    if self._stopping.is_set():
                        continue  # drain set _stopping then _wake; the clear() above may have eaten it
                    try:
                        await asyncio.wait_for(
                            self._wake.wait(),
                            timeout=self.config.poll_interval_seconds,
                        )
                    except TimeoutError:
                        pass
                    continue
                try:
                    leases = await self._engine.claim_due(
                        self._worker_id,
                        max_count=min(self.config.claim_batch_size, free),
                        # Only kinds this pool can run. Without it the unified loop claimed leases of
                        # a kind with no handler (a TOOL_CALL lease left over after the flag was
                        # turned off) and then logged "no handler" and sat on them until they expired.
                        kinds=list(self._dispatch),
                    )
                except Exception:
                    logger.exception("engine claim_loop iteration failed")
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue
                if not leases:
                    self._claims_empty_total += 1
                    self._wake.clear()
                    if self._stopping.is_set():
                        continue  # drain set _stopping then _wake; the clear() above may have eaten it
                    try:
                        await asyncio.wait_for(
                            self._wake.wait(),
                            timeout=self.config.poll_interval_seconds,
                        )
                    except TimeoutError:
                        pass
                    continue
                self._claims_total += len(leases)
                self._reserve_and_dispatch(leases)
        except asyncio.CancelledError:
            return

    def _reserve_and_dispatch(self, leases: list[ClaimLease]) -> None:
        """Reserve in_flight slots for ``leases`` and dispatch each.

        Extracted from the unified claim loop so the Phase 3 stage 7a
        (01a0518b) reserved-split loop below can share it verbatim -
        the reserve-then-dispatch bookkeeping is identical either way,
        only WHICH leases get claimed differs.

        A lease whose ``(kind, id)`` is ALREADY in ``_in_flight`` is a duplicate
        claim and is skipped, and nothing else is done with it: no dispatch, no
        scope, no release, no requeue. ``claim_due`` re-claims any lease whose
        ``expires_at`` has passed, including this worker's own claim, so when a
        heartbeat stalls past the lease TTL while a turn is still running, this
        pool's claim loop claims the same row again. ``worker_id`` is minted per
        pool start, so that can only ever happen here, in the pool still running
        the first execution. Dispatching it would run a second copy of the turn
        and replace the first one's cancel scope. Releasing or requeueing it would
        be worse: a requeue clears ``claimed_by``, the next heartbeat (fenced on
        ``claimed_by``) reports the in-flight key lost and cancels the only
        executor. The re-claim already re-stamped the very row the in-flight
        execution's heartbeat keeps alive, so leaving it alone is the whole fix.

        Pool invariant this relies on, and what keeps the engine's release fence
        at ``claimed_by`` only (no ``claimed_at`` term): a handler's
        ``engine.release`` of its lease completes BEFORE its key leaves
        ``_in_flight`` (the handler releases from inside itself, before
        :meth:`_run_engine`'s ``finally``). So there is never a second in-process
        executor for one key, and an older ``claimed_at`` on a release means the
        sole execution is finishing, not a zombie. It holds on every normal and
        exception path. On abnormal paths (a release that raises, a second cancel
        landing inside the handler's ``finally``, a handler that exits before it
        releases) the key is discarded without the release; no concurrent executor
        results because the task is dead, and the lease, still claimed by this
        worker but no longer heartbeated, expires after one lease TTL and is claimed
        again.

        Residual 1, accepted and bounded by one lease TTL of latency, no work lost:
        a duplicate that lands between the release commit and the discard (the
        window spans the session handler's re-arm) is skipped and leaves a claimed
        lease with no executor; it expires and is claimed again.

        Residual 2, NOT bounded by a TTL and it CAN lose work (tracked as task
        01a1084f): a preempted execution that is still unwinding keeps its key in
        flight. If a peer meanwhile finished the work and re-armed the row, and THIS
        worker's claim loop claimed that fresh row, the duplicate is skipped here and
        the unwinding handler's release (``success=False, drop_lease=True``) then
        matches the fresh row on ``claimed_by`` alone and DELETES it. A session
        recovers only if something re-arms it; a harness lease has no re-arm. It
        needs a stalled heartbeat, a peer's claim and a full claim cycle inside the
        unwind window, so it is rare, and before this change the same sequence ran a
        second executor instead.
        """
        # Reserve in_flight slots immediately before dispatching so
        # back-to-back claim iterations see the correct free count.
        fresh: list[ClaimLease] = []
        for lease in leases:
            key = (lease.kind, lease.entity_id)
            if key in self._in_flight:
                self._duplicate_claims_total += 1
                logger.warning(
                    "duplicate claim of %s/%s skipped: this worker already has it "
                    "in flight (typically its heartbeat stalled past the lease "
                    "TTL); the lease is left untouched",
                    lease.kind, lease.entity_id,
                )
                continue
            if self._stopping.is_set():
                # Shutdown began after this claim was issued (the claim loop was inside
                # claim_due when drain_and_stop set _stopping). Starting the turn would
                # only get it killed at the drain timeout, so hand the lease back for a
                # peer instead of dispatching it.
                self._give_back_unstarted(lease)
                continue
            self._in_flight.add(key)
            fresh.append(lease)
        for lease in fresh:
            handler = self._dispatch.get(lease.kind)
            if handler is None:
                logger.error(
                    "engine_claim_loop: no handler for kind %r, "
                    "entity %s — skipping",
                    lease.kind, lease.entity_id,
                )
                self._in_flight.discard((lease.kind, lease.entity_id))
                continue
            task = asyncio.create_task(
                self._run_engine(lease, handler),
                name=f"engine-{lease.kind}-{lease.entity_id}",
            )
            self._turn_tasks.add(task)
            task.add_done_callback(self._turn_tasks.discard)

    def _give_back_unstarted(self, lease: ClaimLease) -> None:
        """Hand a just-claimed lease back, untouched, because this worker is shutting down.

        ``entity_noop``: the lease never reached a handler, so NO ``on_release`` may run (the
        session adapter's non-park branch would clear a resumable session's park and bump
        ``turn_no`` for a turn that never ran). It is requeued immediately, so a peer's next
        claim takes it. The release is awaited by :meth:`drain_and_stop`.
        """
        logger.info(
            "shutdown in progress: returning just-claimed %s/%s unstarted",
            lease.kind, lease.entity_id,
        )
        task = asyncio.create_task(
            self._release_unstarted(lease),
            name=f"engine-giveback-{lease.kind}-{lease.entity_id}",
        )
        self._unstarted_releases.add(task)
        task.add_done_callback(self._unstarted_releases.discard)

    async def _release_unstarted(self, lease: ClaimLease) -> None:
        try:
            await self._engine.release(
                lease, outcome=ReleaseOutcome(success=True, entity_noop=True),
            )
            self._claims_returned_on_drain_total += 1
        except Exception:
            # A failed give-back leaves the lease claimed by this worker; nothing heartbeats it (it was
            # never in flight), so it expires after one lease TTL and a peer claims it. Slower, not lost.
            self._claim_returns_failed_on_drain_total += 1
            logger.exception(
                "returning unstarted lease %s/%s failed; it will expire and be re-claimed",
                lease.kind, lease.entity_id,
            )

    async def _engine_claim_loop_reserved(self) -> None:
        """Claim loop variant used when config.tool_call_reserved_concurrency
        is set (Phase 3 stage 7a, 01a0518b, ruling C).

        Splits ``concurrency`` into two independently-tracked slices:
        a TOOL_CALL-only reserve, and everything else. Each slice has
        its own free-capacity accounting (based on in_flight entries
        of the matching kind) and its own claim_due call, so a burst
        of tool-call tasks cannot starve session/harness/trigger
        claiming - or vice versa, a general-kind burst starving
        tool-call claiming - the way one shared free count would allow.
        Otherwise mirrors _engine_claim_loop exactly (same batch-size
        cap, same empty-poll backoff, same wake-on-work).
        """
        assert self._engine is not None
        reserve = self.config.tool_call_reserved_concurrency
        assert reserve is not None  # only routed here when set
        general_capacity = self.config.concurrency - reserve
        try:
            while not self._stopping.is_set():
                tool_call_in_flight = sum(
                    1 for kind, _ in self._in_flight if kind == ClaimKind.TOOL_CALL
                )
                general_in_flight = len(self._in_flight) - tool_call_in_flight
                general_free = general_capacity - general_in_flight
                tool_call_free = reserve - tool_call_in_flight

                claimed_any = False
                failed_any = False
                if general_free > 0:
                    got = await self._claim_slice(
                        kinds=[k for k in self._dispatch if k != ClaimKind.TOOL_CALL],
                        max_count=min(self.config.claim_batch_size, general_free),
                    )
                    claimed_any |= got is True
                    failed_any |= got is None
                # Claim tool calls only while something here can run them: a claimed lease with no
                # handler would be logged and abandoned to expire, then claimed again, for ever.
                tool_call_can_claim = tool_call_free > 0 and ClaimKind.TOOL_CALL in self._dispatch
                if tool_call_can_claim and not self._stopping.is_set():
                    got = await self._claim_slice(
                        kinds=[ClaimKind.TOOL_CALL],
                        max_count=min(self.config.claim_batch_size, tool_call_free),
                    )
                    claimed_any |= got is True
                    failed_any |= got is None

                if not claimed_any:
                    if failed_any:
                        # A failed claim_due is not an empty poll, and ``_claim_slice`` already backed off for
                        # one poll interval after it: counting it as empty and waiting again slept twice.
                        continue
                    # Only count as an "empty poll" when at least one
                    # slice actually had free capacity to claim into -
                    # mirrors the unified loop's own free>0-but-empty
                    # accounting; both-slices-full is the reserved
                    # equivalent of its free<=0 short-circuit.
                    if general_free > 0 or tool_call_can_claim:
                        self._claims_empty_total += 1
                    self._wake.clear()
                    if self._stopping.is_set():
                        continue
                    try:
                        await asyncio.wait_for(
                            self._wake.wait(),
                            timeout=self.config.poll_interval_seconds,
                        )
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            return

    async def _claim_slice(
        self, *, kinds: list[ClaimKind], max_count: int,
    ) -> bool | None:
        """One claim_due call restricted to ``kinds``, then dispatch.

        Returns True if anything was claimed, False if the poll came back empty (the reserved loop's
        empty-poll backoff, mirroring the unified loop's own "if not leases" branch), and None if
        ``claim_due`` FAILED: it has already slept one poll interval, and the caller must neither count
        an empty poll nor wait again.
        """
        try:
            leases = await self._engine.claim_due(
                self._worker_id, max_count=max_count, kinds=kinds,
            )
        except Exception:
            logger.exception(
                "engine claim_loop_reserved iteration failed (kinds=%r)", kinds,
            )
            await asyncio.sleep(self.config.poll_interval_seconds)
            return None
        if not leases:
            return False
        self._claims_total += len(leases)
        self._reserve_and_dispatch(leases)
        return True

    async def _run_engine(
        self,
        lease: ClaimLease,
        handler: Callable[[ClaimLease], Coroutine],
    ) -> None:
        """Wrapper that manages _in_flight bookkeeping + a cancel scope
        around a handler call. The scope lets the heartbeat loop preempt a
        running turn of ANY kind when its lease is lost.

        Invariant (see :meth:`_reserve_and_dispatch`): a handler releases its
        lease before this wrapper's ``finally`` discards the key (on every normal
        and exception path), so at most one execution per ``(kind, id)`` exists
        in this pool at any time."""
        key = (lease.kind, lease.entity_id)
        scope = _CancelScope()
        self._active_scopes[key] = scope
        # Lease acquire -> release IS the task boundary (12-s7-design.md
        # section 4): this wrapper brackets every lane's handler.
        task_t0 = time.monotonic()
        task_status = "ok"
        try:
            async with scope:
                await handler(lease)
        except asyncio.CancelledError:
            task_status = "cancelled"
            logger.info(
                "engine handler for %s/%s cancelled (preempted)",
                lease.kind, lease.entity_id,
            )
        except Exception:
            task_status = "error"
            logger.exception(
                "engine handler for %s/%s raised unexpectedly",
                lease.kind, lease.entity_id,
            )
        finally:
            _metrics.worker_tasks_total.labels(
                self._worker_label, lease.kind.value, task_status,
            ).inc()
            _metrics.worker_task_duration_seconds.labels(
                self._worker_label, lease.kind.value, task_status,
            ).observe(time.monotonic() - task_t0)
            self._active_scopes.pop(key, None)
            self._in_flight.discard(key)
            self._wake.set()

    async def _engine_bus_loop(self) -> None:
        """Subscribe to ClaimEngine.watch_ready and wake the claim loop.

        The wake is only a hint (the claim loop also polls), but this loop is
        what notices the watcher died and re-subscribes. What ended the watcher
        decides how it is logged, and every case restarts it after a wait that
        doubles while it keeps failing and starts over once a watcher has
        stayed up for the cap:

        * ``ListenConnectionLost``: a live LISTEN connection was lost (a
          Postgres restart or failover). One WARNING, no traceback.
        * an outage error (``_OUTAGE_ERRORS``: an ``OSError`` such as a refused
          or reset connection or a timeout, or one of asyncpg's connection
          errors such as ``CannotConnectNowError`` while the server is starting
          up or ``ConnectionDoesNotExistError`` mid-LISTEN): it could not
          connect or re-subscribe, which is expected while the server is down
          or restarting. One WARNING naming the cause, no traceback. The
          watcher never reached a subscribed state, so this is not reported
          as a lost LISTEN connection (a connection may well have been lost
          during the subscribe itself, as with 57P01 or 08xxx).
        * anything else is unexpected: an ERROR with its traceback.
        """
        assert self._engine is not None
        backoff = _ENGINE_BUS_BACKOFF_INITIAL_S
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                async for _kind, _entity_id in self._engine.watch_ready():
                    self._wake.set()
                    if self._stopping.is_set():
                        return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if time.monotonic() - started >= _ENGINE_BUS_BACKOFF_MAX_S:
                    backoff = _ENGINE_BUS_BACKOFF_INITIAL_S
                if isinstance(exc, ListenConnectionLost):
                    logger.warning(
                        "engine_bus_loop watch_ready lost its connection (%s); "
                        "restarting in %.1fs",
                        exc, backoff,
                    )
                elif isinstance(exc, _OUTAGE_ERRORS):
                    logger.warning(
                        "engine_bus_loop could not re-subscribe: %s; "
                        "retrying in %.1fs",
                        exc, backoff,
                    )
                else:
                    logger.exception(
                        "engine_bus_loop watch_ready raised; restarting in %.1fs",
                        backoff,
                    )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, _ENGINE_BUS_BACKOFF_MAX_S)
            else:
                backoff = _ENGINE_BUS_BACKOFF_INITIAL_S

    # ---- engine per-kind handlers ----------------------------------------

    async def _run_engine_session(self, engine_lease: ClaimLease) -> None:
        """Handle a SESSION claim from the engine.

        Dispatches to :func:`run_one_session_turn`, building a
        :class:`SessionDispatchDeps` bundle at the call site.  The return
        value is a :class:`ReleaseOutcome` which is passed to
        ``engine.release`` so the engine's lease book-keeping stays
        consistent.
        """
        from primer.int.claim import ReleaseOutcome

        sid = engine_lease.entity_id

        # One shim instance per claim so the session→workspace mapping is
        # isolated to this turn.  The build_executor closure below registers
        # the session_id→workspace_id mapping into the shim so
        # append_message_line can resolve the right workspace.
        io_shim = _WorkspaceIOShim(workspace_registry=self._workspace_registry)

        async def _build_executor_with_shim_registration(
            session: WorkspaceSession,
        ):
            io_shim.register_session(session.id, session.workspace_id)
            return await self._build_session_executor(session)

        def _turn_log_factory(workspace_io, session_id):
            """Build a WorkspaceTurnLogWriter that writes to
            ``<workspace.state_path>/sessions/<sid>/turns.jsonl`` via
            the shim. The shim handles state_path resolution so the
            writer / reader / route all agree even when an operator
            has overridden the default ``.state`` on the template."""
            from primer.observability.turn_log_writer import (
                NoopTurnLogWriter,
                WorkspaceTurnLogWriter,
            )

            workspace_id = io_shim.workspace_id_for(session_id)
            if workspace_id is None:
                return NoopTurnLogWriter()
            # Path is workspace-state-relative; the shim prepends the
            # workspace's own state_path before delegating.
            rel = f"sessions/{session_id}/turns.jsonl"

            async def _append(line: bytes) -> None:
                await io_shim.append_state_line(workspace_id, rel, line)

            async def _read_existing() -> bytes:
                return await io_shim.read_state_file(workspace_id, rel)

            return WorkspaceTurnLogWriter(
                append_line=_append,
                read_existing=_read_existing,
            )

        deps = SessionDispatchDeps(
            storage_provider=self._storage,
            workspace_io=io_shim,
            event_bus=self._event_bus,
            build_executor=_build_executor_with_shim_registration,
            turn_log_writer_factory=_turn_log_factory,
            channel_dispatcher=self._channel_dispatcher,
            workspace_registry=self._workspace_registry,
            artifact_registry=self._artifact_storage_registry,
            scheduler=self._scheduler,
            claim_engine=self._engine,
        )

        outcome = ReleaseOutcome(success=False, drop_lease=True)
        try:
            # Load the row first so a resumable park dispatches to the
            # resume branch instead of a normal turn. ``self._storage`` is
            # always present in production; some pool unit-tests construct
            # the pool with ``storage=None`` and patch run_one_session_turn,
            # so tolerate a missing provider by falling through to the
            # normal-turn path.
            session_row = None
            if self._storage is not None:
                session_storage = self._storage.get_storage(WorkspaceSession)
                session_row = await session_storage.get(sid)
            if session_row is not None and session_row.parked_status == "resumable":
                # Cancel/end-while-parked: a cancelled or already-ended
                # resumable session ends instead of resuming (spec error
                # handling 5). run_one_session_turn applies the same guard
                # for the normal path at dispatch.py:137-144; the resume
                # branch bypasses that function, so re-check here.
                if session_row.status == SessionStatus.ENDED:
                    outcome = ReleaseOutcome(success=True, drop_lease=True)
                elif session_row.cancel_requested:
                    outcome = await self._end_session(session_row, reason="cancelled")
                elif session_row.pause_requested:
                    # Pause-while-parked: the operator paused a resumable
                    # session. Transition to PAUSED and preserve the park
                    # instead of resuming, so a later /resume re-arms the lease
                    # and replays the hook. The normal-turn path applies the
                    # same guard in run_one_session_turn; the resume branch
                    # bypasses that function, so re-check here (e2e t0867).
                    outcome = await self._pause_session(session_row)
                elif session_resume_coordinator.resume_already_applied(session_row):
                    # The resume of THIS park already ran and its release never committed (abandoned at the
                    # pool's bound, or raised, so it rolled back and the park columns are still there; ticket
                    # 01a10b54-425b). Running the handler again would inject the reply a second time and run
                    # an approved tool a second time, so it does not run: the outcome is the one the handler
                    # returned (success, lease kept), and this claim's own release clears the park and
                    # applies the lost turn_no bump, once (the adapter fences it). It sits after the ENDED,
                    # cancel and pause exits, which are decided from the row as before, and before the
                    # workspace check and the Stop clear, which belong to a park being resolved NOW.
                    logger.warning(
                        "session %s: the resume of the park stamped %s was already applied (its release did "
                        "not commit); not running the handler again, releasing the park",
                        sid, session_row.parked_at,
                    )
                    _metrics.session_resume_noop_total.inc()
                    outcome = ReleaseOutcome(success=True, drop_lease=False)
                else:
                    # A workspace this deployment refuses (ticket 01a1072f) is found HERE, before the Stop is
                    # cleared, a handler is chosen or anything is reported: the handlers emit
                    # ``session.resumed`` before they load the workspace, and the adapter's release would
                    # clear the park the human just answered. The cancel and pause cases above never need
                    # the workspace, so they were decided first.
                    if self._workspace_registry is not None:
                        await self._workspace_registry.check_workspace_allowed(session_row.workspace_id)
                    # A park is being resolved (an approval or an answer, a tool_wait batch, or
                    # either on a graph session; all of them pass through here). A Stop recorded
                    # before that must not outlive it: it would be honoured by the first poll of
                    # the continuation and kill it before its first token. A later explicit
                    # human action wins over an earlier Stop.
                    await clear_interrupt_for_resume(session_storage, sid)
                    handler = self._select_resume_handler(session_row)
                    outcome = await handler(engine_lease, session_row)
            else:
                outcome = await run_one_session_turn(engine_lease, deps)
        except asyncio.CancelledError:
            # Preempt: the heartbeat loop hard-cancelled this turn because
            # the lease was lost (see _heartbeat_loop -> scope.cancel). Two
            # causes are indistinguishable from the CancelledError alone:
            #   (a) a REST cancel set cancel_requested=True and dropped the
            #       lease -> the session must converge to ENDED/cancelled,
            #       otherwise the normal-turn path leaves it stuck RUNNING
            #       (the graceful in-stream cancel only wins under a fast
            #       LLM; a slow completion is killed here first), or
            #   (b) a genuine lease STEAL/expiry: another worker legitimately
            #       took over (cancel_requested is False). We MUST NOT end the
            #       session in that case or we corrupt the multi-worker
            #       handoff -- the owning worker drives it to terminal.
            # Disambiguate on the FRESH row's cancel_requested, end only on
            # (a), and ALWAYS re-raise so _run_engine still logs/cleans up.
            try:
                if self._storage is not None:
                    session_storage = self._storage.get_storage(WorkspaceSession)
                    fresh = await session_storage.get(sid)
                    if (
                        fresh is not None
                        and fresh.cancel_requested
                        and fresh.status != SessionStatus.ENDED
                    ):
                        outcome = await self._end_session(fresh, reason="cancelled")
            except Exception:
                # A storage error here must not mask task cancellation;
                # mirror dispatch.py's failure-isolation pattern: log and
                # fall through to the re-raise below.
                logger.exception(
                    "preempt-cancel convergence for session %s failed", sid,
                )
            raise
        except WorkspaceRefusedError as refused:
            # The deployment refuses this session's workspace: the turn fails and the session stays resumable,
            # park kept, never ended and never workspace_lost (see pause_session_for_refused_workspace).
            if self._storage is not None:
                outcome = await pause_session_for_refused_workspace(
                    self._storage.get_storage(WorkspaceSession), sid, refused,
                )
        except Exception:
            logger.exception(
                "run_one_session_turn for session %s raised unexpectedly",
                sid,
            )
        finally:
            # ``_in_flight`` bookkeeping is owned by the ``_run_engine``
            # wrapper's finally (the session path is always dispatched
            # through it); discarding here too would be redundant.
            self._wake.set()
            try:
                await self._release_lease(engine_lease, outcome)
            except Exception:
                logger.exception(
                    "_run_engine_session: engine.release for %s failed", sid,
                )
            else:
                # C1 fix: a wake_session() steer that lands while this
                # worker holds the lease sets turn_status="claimable" but
                # (unable to touch an already-claimed lease) can't create a
                # new claim itself. Every ReleaseOutcome dispatch.py returns
                # for a session drops the lease unconditionally, so without
                # this the queued turn is stranded: no lease exists for the
                # engine to ever re-claim. Re-arm AFTER the release (once
                # the old lease is actually gone) so a genuinely queued
                # turn always gets a fresh claim.
                await self._maybe_rearm_session(sid)

    def _select_resume_handler(
        self, session_row: WorkspaceSession,
    ) -> Callable[[ClaimLease, WorkspaceSession], Coroutine]:
        """Which resume handler pool.py's resume branch should call.

        Phase 3 stage 7a (01a0518b): a tool_wait park
        (``session_row.parked_state`` kind ``"tool_wait"``) is NOT
        ``Yielded``-shaped — ``session_resume_coordinator``'s own
        ``ParkedState.from_jsonable`` rehydration assumes exactly that
        shape (built from a ``Yielded`` sentinel), so a tool_wait park
        must never reach it. Peeked here, BEFORE
        ``session_resume_coordinator`` is ever entered, so its own
        contract stays "Yielded-shaped blobs only", un-widened — pool.py
        already owns what-kind-of-work-is-this dispatch (see
        ``_select_claim_loop``, the same pattern). A defensive tripwire
        also guards the top of ``resume_engine_session`` itself in case
        this routing is ever bypassed (see that function's own
        docstring) — belt and braces, not redundancy: the tripwire turns
        a hypothetical mis-route from a silent misparse into a loud
        error, this function is what actually prevents it from
        happening in the first place.
        """
        parked_state = session_row.parked_state or {}
        if parked_state.get("kind") == "tool_wait":
            return self._resume_engine_tool_wait
        return self._resume_engine_session

    async def _release_lease(self, lease: ClaimLease, outcome: ReleaseOutcome) -> None:
        """Hand a running execution's lease back through the engine.

        Every handler releases through here, not ``self._engine.release`` directly: it first marks
        the execution's scope ``lease_returned``, so a lost-lease verdict from a heartbeat that was
        already in flight when the release ran is not delivered to an execution that has given its
        lease back (see :meth:`_CancelScope.mark_lease_returned`). The mark comes BEFORE the call,
        not after it: the engine answers "not yours" as soon as the release commits, which can be
        before the call returns to this task, and the release's own post-commit hooks (the tool-call
        wake) must not be cancelled either. If the release itself fails the lease is still held and
        still heartbeated until the wrapper discards the key, which follows immediately.

        The release is BOUNDED (``_release_timeout_seconds``, the lease TTL minus one heartbeat interval). Once the
        scope is marked, a lost-lease verdict can no longer push a release that hangs (a stuck connection after the
        lease was genuinely lost), so without a bound only the drain timeout would end it. The bound is no longer than
        that because a slow release is not harmless to the worker's OTHER leases: on Postgres the heartbeat's single
        ``UPDATE`` waits on this release's row lock, so none of them is refreshed until the release ends, and each can
        lapse one TTL after its last refresh (see ``__init__``). On timeout the release is cancelled and counted.

        The bound is BEST-EFFORT, not firm. ``asyncio.timeout`` only cancels the awaiting task. What asyncpg 0.31 then
        does (read from its source, not exercised against a live server here): it stops the statement's own
        ``command_timeout`` timer, sends the server a cancel request over a separate connection and lets the
        ``CancelledError`` through; the release's ``conn.transaction()`` exit then sends ``ROLLBACK``, and asyncpg runs
        no statement on that connection before the server has answered the cancelled one, a wait with NO timeout. So
        this task stays inside the release, and the server keeps the row lock (the heartbeat stays stalled), until the
        server answers (normally at once: the cancel aborts the statement). The storage pool's ``command_timeout``
        (``PoolConfig.acquire_timeout``, 30 s by default, passed in ``primer/storage/postgres.py``) does not bound that
        wait; it can end the release first only if one statement of it has already run for 30 s, which needs a bound
        above 30 s, and then it is the engine's own ``TimeoutError``. For a server that has gone away, the backstop is
        the kernel TCP keepalive ``keepalive_init_hook`` (``primer/storage/_pg_pool.py``) switches on for every storage
        pool connection (``tcp_keepalive_idle_seconds`` 60, interval 10, count 3 by default: a vanished peer is noticed
        within about 90 s; off when the idle setting is 0); it does nothing for a live server that is merely slow, and
        asyncpg's connection-lost path fails the statement's waiter but, as read, not that cancel wait, so whether a
        lost connection ends it is not established. Work ``on_release`` awaits that is not a statement (the session
        adapter's terminal-record workspace write) stops only as far as that I/O honours cancellation.

        A timed-out release has an UNKNOWN outcome, not a failed one: the bound also covers what follows the commit
        (the post-release hook, the COMMIT's own reply), so the release may well have committed. One bounded probe
        (``has_lease``, within ``_release_probe_timeout_seconds``) decides. Lease row ABSENT: the release evidently
        committed (or something else removed the lease; either way there is no lease left to wait for), so this
        returns normally and the caller runs exactly what it runs after a release that returned (the session handler
        re-arms a queued steer, which would otherwise be stranded with no lease); counted again in
        ``_release_timeouts_committed_total``. Lease row PRESENT, or the probe failed or timed out (unknown): the
        ``TimeoutError`` propagates and the caller treats it like any failed release (unless the caller is unwinding a
        cancel, releasing from its ``finally``: then that cancel propagates instead, so the task still ends cancelled
        and ``worker_tasks_total`` labels it so, not ``error``). The key then leaves ``_in_flight``, nothing heartbeats
        the lease, it expires after one TTL and a peer (or this worker) re-claims it.

        That is NOT a harmless retry when the work had finished, unlike a drain hand-back (an unstarted lease). A
        bound that fired BEFORE the commit rolled back what ``on_release`` wrote (a session's ``turn_no`` bump and
        ``last_turn_at``, or its park columns; a harness's cleared ``pending_operation``; a trigger's next fire time),
        so the re-claim finds the entity runnable and does the work AGAIN: a harness operation runs again; a trigger
        fires again. A session turn that COMPLETED is the exception: it recorded ``completed_turn_no`` before its
        release, so the re-claim finds ``completed_turn_no == turn_no`` and ``run_one_session_turn`` takes its no-op
        path (no model call; its own release applies the lost bump; see ``_noop_if_turn_already_completed``). A
        session's park release that rolls back is not covered: the re-claim runs a fresh turn. A resume release that
        rolls back is covered for the continue path only (``resumed_park_at``, ticket 01a10b54-425b: the re-claim does
        not run the handler again); a resume that re-parks still runs again (ticket 01a1206e-0acc). A failed session release's terminal ERROR record is a workspace write the database transaction
        does not cover, so the rollback does not undo it and it can be written twice. This is the cost of a bound
        below the lease TTL: a shorter bound abandons more releases that are slow but alive, while the longer bound it
        replaced let a slow release stall the heartbeat long enough to duplicate the turns of the worker's OTHER leases
        instead.

        The probe UNDERCOUNTS committed timeouts: the contract has no read of who holds a lease (``heartbeat`` would
        tell, but it writes, refreshes the TTL and on Postgres waits on the very row lock an open release holds), so a
        committed release that KEEPS its row (``drop_lease=False``: the trigger handler, a resumed session's
        continuation) leaves it unclaimed, reads as present and is handled as abandoned. That is harmless for the
        lease (the row is already claimable) but such a release is not counted as committed and gets no post-release
        path (a trigger task is labelled ``error``).

        WHERE IT MAY BE CALLED FROM. Whether the caller is unwinding a cancel is read from ``sys.exception()``, the
        exception being handled where this is awaited (including by any coroutine up the ``await`` chain). Call it
        from ordinary code (nothing is being handled: ``None``), from a ``finally`` (the exception it is unwinding,
        if any) or from an ``except`` block that re-raises what it caught. NEVER from an ``except`` that swallows a
        ``CancelledError`` (or ``BaseException``), nor from code awaited inside one: ``sys.exception()`` reports the
        swallowed cancel there, and a timed-out release would raise it again.
        """
        in_flight = sys.exception()   # a cancel the caller's ``finally`` is unwinding, if it releases from one
        scope = self._active_scopes.get((lease.kind, lease.entity_id))
        if scope is not None:
            scope.mark_lease_returned()
        try:
            async with asyncio.timeout(self._release_timeout_seconds) as bound:
                await self._engine.release(lease, outcome=outcome)
        except TimeoutError:
            if not bound.expired():
                raise  # the engine's own timeout (a command timeout), not this bound: not ours to count
            self._release_timeouts_total += 1
            try:
                async with asyncio.timeout(self._release_probe_timeout_seconds):
                    gone = not await self._engine.has_lease(lease.kind, lease.entity_id)
            except Exception:  # the probe's own timeout too: the outcome stays unknown, treated as still there
                logger.warning(
                    "probing the lease of %s/%s after its release timed out failed", lease.kind, lease.entity_id,
                    exc_info=True,
                )
                gone = False
            if gone:
                self._release_timeouts_committed_total += 1
                logger.error(
                    "releasing %s/%s did not finish within %.1fs; outcome unknown, but its lease row is gone, so it "
                    "evidently committed: carrying on as after a release",
                    lease.kind, lease.entity_id, self._release_timeout_seconds,
                )
                return
            logger.error(
                "releasing %s/%s did not finish within %.1fs; outcome unknown and its lease row is still there (or the "
                "probe could not tell): abandoning it (the lease expires and a peer re-claims it)",
                lease.kind, lease.entity_id, self._release_timeout_seconds,
            )
            if isinstance(in_flight, asyncio.CancelledError):
                raise in_flight   # the cancel goes on: a TimeoutError would replace it, and its task metric label
            raise

    async def _maybe_rearm_session(self, session_id: str) -> None:
        """Re-arm a fresh SESSION claim lease if a turn is still queued.

        Every session ``ReleaseOutcome`` drops the lease unconditionally
        (see ``primer.session.dispatch``), so instead of keeping the old lease
        alive this re-upserts a brand-new one, once release has actually
        dropped the old one -- calling upsert first would just touch the
        (still held-by-us, about to be dropped) lease's priority and be
        wiped out the instant release runs.

        ``run_one_session_turn`` flips ``turn_status`` to "running"
        unconditionally when a turn starts (whatever it was) and the turn's
        cleanup returns it to "idle" unless a wake set "claimable"
        meanwhile, so a lingering ``turn_status == "claimable"`` at this
        point means a ``wake_session()`` steer landed during (or right
        after) the turn that just released -- not a stale, already-serviced
        signal -- or the completed-turn no-op path found an unanswered
        input and set it (``_noop_if_turn_already_completed``).

        No-ops when the session ended (a restart is required, and reset
        clears turn_status itself) or is not RUNNING/WAITING (e.g.
        PAUSED, or parked -- its own resume event re-arms it, not this
        generic path, so re-arming here would race the resumable-park
        dispatch above).
        """
        if self._storage is None:
            return
        session_storage = self._storage.get_storage(WorkspaceSession)
        try:
            fresh = await session_storage.get(session_id)
        except Exception:
            logger.exception(
                "_maybe_rearm_session: failed to read session %s", session_id,
            )
            return
        if fresh is None or fresh.parked_status is not None:
            return
        if fresh.status not in (SessionStatus.RUNNING, SessionStatus.WAITING):
            return
        if fresh.turn_status != "claimable":
            return
        try:
            await self._engine.upsert(ClaimKind.SESSION, session_id)
        except Exception:
            logger.exception(
                "_maybe_rearm_session: claim_engine.upsert failed for %s",
                session_id,
            )

    async def _end_session(self, session, *, reason: str):
        """Write a terminal ENDED status to the session row and return a
        drop-lease outcome. Mirrors dispatch.py's cancel/end pattern so the
        engine path ends sessions without the scheduler."""
        from primer.int.claim import ReleaseOutcome

        storage = self._storage.get_storage(WorkspaceSession)
        fresh = await storage.get(session.id)
        if fresh is not None:
            ended = fresh.model_copy(update={
                "status": SessionStatus.ENDED,
                "ended_reason": reason,
                "ended_at": datetime.now(timezone.utc),
            })
            await storage.update(ended)
            # Every terminal exit of the turn applies a switch queued on the session (dispatch.py's drain
            # checkpoint); this one has no turn behind it, so it applies the switch itself. Without it a
            # switch queued on a parked session survives a failed resume on the ENDED row, and after a reopen
            # the user's next message is answered by the OUTGOING binding. Best effort, like the checkpoint.
            from primer.session.dispatch import apply_queued_binding_switch, realize_queued_steer

            io_shim = _WorkspaceIOShim(workspace_registry=self._workspace_registry)
            io_shim.register_session(session.id, session.workspace_id)
            await apply_queued_binding_switch(
                storage_provider=self._storage, workspace_io=io_shim, session_id=session.id,
                # the row is ENDED by this very write but still carries its park columns until the release
                guard={"status": [SessionStatus.ENDED.value]},
            )
            # ... and then realizes ONE queued steer, in the checkpoint's order (switch first, so the follow-up runs
            # under the incoming binding). A steer sent to a parked session is queued (route_steer counts it as busy);
            # without this it waited behind the ended session for some later message, or forever. It reopens the ended
            # session through wake_session and arms a turn; the pool re-arms the claim after the release.
            await realize_queued_steer(
                storage_provider=self._storage, workspace_id=session.workspace_id, session_id=session.id,
                scheduler=self._scheduler, claim_engine=self._engine, workspace_registry=self._workspace_registry,
                event_bus=self._event_bus,
            )
        else:
            # 01a08bf0: "vanished because something else already ended it"
            # and "vanished unexpectedly" are genuinely indistinguishable
            # here -- fresh is None carries no reason. success=True is left
            # as-is deliberately rather than hardened, because it is
            # observably inert: SessionClaimAdapter.on_release does its OWN
            # independent re-fetch of this row and returns immediately if
            # that ALSO finds nothing (its first read of the row), so a
            # genuinely-gone row never reaches the
            # outcome.success branch that would otherwise write a terminal
            # error record or bump turn_no. This is a COINCIDENTAL property
            # of on_release's re-check, not a designed guarantee -- see
            # test_end_session_vanished_row_mislabel_is_inert_via_on_release_recheck
            # in tests/worker/test_pool.py, which would need updating if
            # on_release's re-check ever changes.
            logger.warning(
                "end_session: row %s vanished before terminal write (reason=%r)",
                session.id, reason,
            )
        # success=True so on_release does not write a terminal error record;
        # drop_lease=True so the ended session is not re-claimed.
        return ReleaseOutcome(success=True, drop_lease=True)

    async def _pause_session(self, session):
        """Write a PAUSED status to a resumable session and return a
        park-preserving outcome. Mirrors _end_session, but keeps the park:
        preserve_park=True tells on_release to leave parked_status (still
        'resumable') and parked_state untouched, so a later /resume re-arms
        the lease and replays the hook. It does not leave turn_no alone: on
        a successful release the adapter's preserve-park branch still bumps
        turn_no and stamps last_turn_at (SessionClaimAdapter.on_release)."""
        from primer.int.claim import ReleaseOutcome

        storage = self._storage.get_storage(WorkspaceSession)
        fresh = await storage.get(session.id)
        if fresh is not None:
            paused = fresh.model_copy(update={"status": SessionStatus.PAUSED})
            await storage.update(paused)
        else:
            # 01a08bf0: same reasoning as _end_session's vanished-row branch
            # above -- success=True is left as-is; the re-fetch at the top of
            # SessionClaimAdapter.on_release makes this inert today,
            # coincidentally rather than by design.
            logger.warning(
                "pause_session: row %s vanished before pause write", session.id,
            )
        # drop_lease=True so the paused session is not re-claimed until /resume
        # re-arms it; preserve_park=True so on_release keeps the park columns.
        return ReleaseOutcome(
            success=True, drop_lease=True, preserve_park=True,
        )

    async def _write_approval_record_for_session(
        self, *, session, blob: dict, payload,
    ) -> None:
        return await session_resume_coordinator.write_approval_record_for_session(
            self, session=session, blob=blob, payload=payload,
        )

    async def _write_approval_record_for_graph(
        self, *, session, checkpoint: dict, tcid, payload,
    ) -> None:
        return await graph_resume_coordinator.write_approval_record_for_graph(
            self, session=session, checkpoint=checkpoint, tcid=tcid, payload=payload,
        )

    async def _resume_engine_session(self, engine_lease, session):
        return await session_resume_coordinator.resume_engine_session(
            self, engine_lease, session,
        )

    async def _resume_engine_tool_wait(self, engine_lease, session):
        """Resume a session parked on a tool_wait batch (Phase 3 stage
        7a, 01a0518b). Routed here by ``_select_resume_handler`` -
        never reaches ``session_resume_coordinator``, whose own
        rehydration assumes a ``Yielded``-shaped park."""
        return await tool_wait_resume_coordinator.resume_engine_tool_wait(
            self, engine_lease, session,
        )

    async def _inject_resume_and_continue(
        self, session, executor, parked, tool_result_part,
    ):
        return await session_resume_coordinator.inject_resume_and_continue(
            self, session, executor, parked, tool_result_part,
        )

    def _build_invocation_services(self, session, workspace, executor, tool_manager):
        return session_resume_coordinator.build_invocation_services(
            self, session, workspace, executor, tool_manager,
        )

    def _repark_continuation(self, session, parked, outcome):
        return session_resume_coordinator.repark_continuation(
            self, session, parked, outcome,
        )

    async def _resume_graph_engine(self, session, parked):
        return await graph_resume_coordinator.resume_graph_engine(
            self, session, parked,
        )

    def _graph_value_yield_toolcall(self, checkpoint, tcid) -> bool:
        return graph_resume_coordinator.graph_value_yield_toolcall(
            self, checkpoint, tcid,
        )

    def _graph_nested_agent_yield(self, checkpoint, tcid):
        return graph_resume_coordinator.graph_nested_agent_yield(
            self, checkpoint, tcid,
        )

    async def _resume_graph_continuation(
        self, session, parked, checkpoint, ay, payload, workspace, executor,
    ):
        return await graph_resume_coordinator.resume_graph_continuation(
            self, session, parked, checkpoint, ay, payload, workspace, executor,
        )

    def _repark_graph_continuation(self, session, parked, checkpoint, ay, outcome):
        return graph_resume_coordinator.repark_graph_continuation(
            self, session, parked, checkpoint, ay, outcome,
        )

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id):
        return await graph_resume_coordinator.graph_agent_tool_result(
            self, checkpoint, tcid, payload, session_id=session_id,
        )

    async def _persist_resume_tool_result_record_for_graph(
        self, *, session, checkpoint, tcid, agent_tool_result,
    ) -> None:
        return await graph_resume_coordinator.persist_resume_tool_result_record_for_graph(
            self, session=session, checkpoint=checkpoint, tcid=tcid,
            agent_tool_result=agent_tool_result,
        )

    def _repark_graph_outcome(self, session, repark, *, node_tool_call_seq=None):
        return graph_resume_coordinator.repark_graph_outcome(
            self, session, repark, node_tool_call_seq=node_tool_call_seq,
        )

    def _repark_resumed_yield_outcome(self, session, parked, yld):
        return session_resume_coordinator.repark_resumed_yield_outcome(
            self, session, parked, yld,
        )

    async def _run_engine_harness(self, engine_lease: ClaimLease) -> None:
        return await engine_handlers.run_engine_harness(self, engine_lease)

    async def _run_engine_trigger(self, engine_lease: ClaimLease) -> None:
        return await engine_handlers.run_engine_trigger(self, engine_lease)

    async def _cancel_loop(self) -> None:
        """Drain cancel notifications. When a sid arrives that this worker
        holds an active scope for, fire scope.cancel_once(reason). The reason
        string is informational; the running turn inspects the Session row
        to determine cancel-vs-pause routing.

        ``cancel_once``, not ``cancel``: a second cancel (a double-clicked
        Cancel sends two NOTIFYs) landing while the session handler is still
        converging the preempted session to ENDED would skip that convergence
        and strand the session (see :class:`_CancelScope`).

        NOTIFY is not durable, so this loop alone can miss a cancel (while the
        watcher reconnects, or while a dead connection has not been detected
        yet); :meth:`_cancel_reconcile_loop` is the safety net for those.

        Restart-on-failure pattern mirrors :meth:`_notify_loop`.
        """
        backoff = 1.0
        while not self._keepalive_over():
            try:
                # aclosing: the early return below (or any other exit from
                # the loop body) would otherwise abandon a SUSPENDED async
                # generator. CPython's async-generator finalizer would still
                # close it on the next loop iteration once nothing references
                # it, so the scheduler's pooled LISTEN connection is released
                # either way; aclosing makes that release deterministic and
                # inline instead of one loop tick late, and does not depend
                # on refcount-driven finalization.
                async with contextlib.aclosing(self._cancel_iter()) as cancel_iter:
                    async for sid in cancel_iter:
                        scope = self._active_scopes.get((ClaimKind.SESSION, sid))
                        if scope is not None:
                            scope.cancel_once("user_signal")
                        if self._keepalive_over():
                            return
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "cancel_loop watch_cancel raised; restarting in %.1fs",
                    backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, 30.0)
            else:
                # Generator exited cleanly. Yield to the event loop before
                # restarting — prevents a tight spin when the scheduler's
                # cancel generator immediately exits (e.g. in tests where
                # no session cancellations are expected).
                backoff = 1.0
                await asyncio.sleep(0)

    def _cancel_iter(self):
        """Resolve the cancel iterator. Both InMemoryScheduler.watch_cancel
        (public test helper) and PostgresScheduler._watch_cancel (private,
        same shape) yield session_ids from the cancel channel."""
        sched = self._scheduler
        if hasattr(sched, "watch_cancel"):
            return sched.watch_cancel(self._worker_id)
        if hasattr(sched, "_watch_cancel"):
            return sched._watch_cancel(self._worker_id)  # noqa: SLF001
        # Fallback: scheduler doesn't expose a cancel channel.
        async def _empty():
            if False:
                yield
        return _empty()

    async def _reconcile_cancels(self) -> int:
        """Cancel running sessions whose row says a cancel is pending.

        ``cancel_session`` records ``cancel_requested`` on the session row and
        then sends a ``session_cancel`` NOTIFY, which is what lets
        :meth:`_cancel_loop` hard-preempt a turn blocked in a long LLM or tool
        call. The NOTIFY can be lost (the watcher is reconnecting, a half-open
        connection has not been detected yet, startup before the first LISTEN),
        and then the API answered 200 while the turn kept running until it next
        yielded an event. The row is the truth, so this re-reads it for every
        SESSION scope this worker holds. It is idempotent, so a cancel that
        arrives both ways is cancelled once (``cancel_once``).

        Only ``cancel_requested`` is checked: that is the one flag
        ``cancel_session`` signals through the NOTIFY. Interrupt (Stop) never had
        this path. Returns how many scopes it cancelled. A storage error for one
        session is logged and skipped; it never propagates.
        """
        reconciled = 0
        for (kind, sid), scope in list(self._active_scopes.items()):
            if kind is not ClaimKind.SESSION or scope.cancelled:
                continue
            try:
                row = await self._load_session(sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cancel reconcile: could not read session %s, will retry: %s", sid, exc,
                )
                continue
            if row is None or not row.cancel_requested or row.status == SessionStatus.ENDED:
                continue
            # The turn may have finished (or been cancelled by a NOTIFY) while
            # the row was being read.
            if self._active_scopes.get((kind, sid)) is not scope:
                continue
            if scope.cancel_once("user_signal"):
                self._cancels_reconciled_total += 1
                reconciled += 1
                logger.info(
                    "cancel for session %s reached this worker through the session "
                    "row, not the NOTIFY (a NOTIFY was missed or is still queued)",
                    sid,
                )
        return reconciled

    async def _cancel_reconcile_loop(self) -> None:
        """Run :meth:`_reconcile_cancels` every ``heartbeat_interval_seconds``.

        Its own task, not a step of :meth:`_heartbeat_loop`: the row read can
        block up to the pool's ``command_timeout`` (30s), which inside the
        heartbeat loop would delay lease heartbeats past the lease TTL and cost
        leases. The cadence deliberately follows the heartbeat interval (no
        separate setting), so a cancel whose NOTIFY was lost is preempted within
        about that long, whichever way it was lost.
        """
        try:
            while not self._keepalive_over():
                await asyncio.sleep(self.config.heartbeat_interval_seconds)
                if self._keepalive_over():
                    return
                try:
                    await self._reconcile_cancels()
                except Exception:
                    logger.exception("cancel_reconcile_loop iteration failed")
        except asyncio.CancelledError:
            return

    # ---- per-turn execution -----------------------------------------------

    async def _load_session(self, sid: str) -> WorkspaceSession | None:
        """Fetch the persisted WorkspaceSession row. Override in tests via monkeypatch."""
        sp_storage = self._storage.get_storage(WorkspaceSession)
        return await sp_storage.get(sid)

    async def _load_workspace_for_persist(self, workspace_id: str):
        """Fetch the live workspace handle for the current turn. Override in tests.

        Name kept for back-compat with test monkeypatches; this method is
        no longer tied to ``persist_turn`` (removed).
        """
        return await self._workspace_registry.get_workspace(workspace_id)

    async def _build_executor(self, session: WorkspaceSession, workspace):
        return await executor_builders.build_executor(self, session, workspace)

    async def _build_session_executor(self, session: WorkspaceSession):
        return await executor_builders.build_session_executor(self, session)

    def _build_graph_invocation_services(
        self,
        *,
        workspace,
        workspace_session,
        graph_session_id: str,
        initiated_by=None,
    ):
        return executor_builders.build_graph_invocation_services(
            self,
            workspace=workspace,
            workspace_session=workspace_session,
            graph_session_id=graph_session_id,
            initiated_by=initiated_by,
        )

    async def _build_agent_executor(self, session: WorkspaceSession, workspace):
        return await executor_builders.build_agent_executor(self, session, workspace)

    async def _build_graph_executor(self, session: WorkspaceSession, workspace):
        return await executor_builders.build_graph_executor(self, session, workspace)

    async def _resolve_llm(self, agent, override_profile_id=None):
        from primer.model_profile import resolve_llm
        return await resolve_llm(
            self._storage, self._provider_registry,
            default_profile_id=agent.model.profile_id,
            override_profile_id=override_profile_id,
        )

    def _infer_post_turn_status(self, executor, session: WorkspaceSession) -> SessionStatus:
        return executor_builders.infer_post_turn_status(self, executor, session)


# Imported at the bottom so the helper modules (which import names defined
# above, e.g. ``_toolset_ids_from_scoped`` / ``_TurnDriver`` / ``WorkerPool``)
# resolve against a fully-initialised ``primer.worker.pool`` module and the
# import cycle never bites. The WorkerPool delegators reference these by
# attribute at call time.
from primer.worker import executor_builders  # noqa: E402
from primer.worker import engine_handlers  # noqa: E402
from primer.worker import graph_resume_coordinator  # noqa: E402
from primer.worker import session_resume_coordinator  # noqa: E402
from primer.worker import tool_wait_resume_coordinator  # noqa: E402
