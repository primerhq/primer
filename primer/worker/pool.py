"""Background worker pool — claims sessions, chats and harnesses and runs one turn each.

Claim loop architecture:
* A single ``_engine_claim_loop`` and ``_engine_bus_loop`` handle all claim
  kinds (session, chat, harness) via the injected ``ClaimEngine``.

One unified ``_in_flight: set[tuple[ClaimKind, str]]`` tracks all in-flight
items regardless of kind.  Capacity: ``free = max_concurrency - len(_in_flight)``.

See ``docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md``
§6 for the full design.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.int.claim import ClaimKind
from primer.int.claim import Lease as ClaimLease
from primer.int.scheduler import (
    Scheduler,
)
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import WorkspaceSession, SessionStatus
from primer.worker.turn import _CancelScope
from primer.worker.drivers import _GraphTurnDriver, _TurnDriver  # noqa: F401  re-export
from primer.worker.io_shim import _WorkspaceIOShim
from primer.worker._toolset_ids import _toolset_ids_from_scoped  # noqa: F401  re-export

from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn

if TYPE_CHECKING:
    from primer.agent.approval import ApprovalResolver
    from primer.api.registries import ProviderRegistry, WorkspaceRegistry
    from primer.graph.router import RouterRegistry
    from primer.int.claim import ClaimEngine
    from primer.int.event_bus import EventBus
    from primer.int.storage_provider import StorageProvider
    from primer.chat.tick_router import ChatTickRouter

logger = logging.getLogger(__name__)


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
        chat_tick_router: "ChatTickRouter | None" = None,
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
        self._chat_tick_router = chat_tick_router
        self._artifact_storage_registry = artifact_storage_registry
        self._engine = engine

        self._worker_id: str = ""
        self._tasks: list[asyncio.Task] = []
        self._active_scopes: dict[tuple[ClaimKind, str], _CancelScope] = {}

        # Unified in-flight tracking — one set for all claim kinds.
        # (ClaimKind, entity_id) tuples for all kinds.
        self._in_flight: set[tuple[ClaimKind, str]] = set()

        # Strong references to in-flight per-turn tasks so the GC does not
        # silently collect them between create_task and the first await.
        self._turn_tasks: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()

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

    async def start(self) -> None:
        self._worker_id = f"wrk-{uuid.uuid4().hex[:12]}"
        # Tell the scheduler our lease TTL so its claim/heartbeat SQL
        # uses the right interval. Not all impls expose the setter
        # (the ABC doesn't require it), so guard with a try/except.
        try:
            self._scheduler.lease_ttl_seconds = self.config.lease_ttl_seconds  # type: ignore[attr-defined]
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
            ClaimKind.CHAT:    self._run_engine_chat,
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
        ]
        self._engine_claim_task = asyncio.create_task(
            self._engine_claim_loop(),
            name=f"engine-claim-{self._worker_id}",
        )
        self._engine_bus_task = asyncio.create_task(
            self._engine_bus_loop(),
            name=f"engine-bus-{self._worker_id}",
        )

    async def drain_and_stop(self, timeout: float | None = None) -> None:
        self._stopping.set()
        # Wake any sleeping claim loops so they see the stopping flag.
        self._wake.set()
        try:
            await self._scheduler.drain_worker(self._worker_id)
        except Exception:
            logger.exception("drain_worker failed for %s", self._worker_id)
        drain_timeout = (
            timeout
            if timeout is not None
            else float(self.config.drain_timeout_seconds)
        )
        deadline = asyncio.get_event_loop().time() + drain_timeout
        while self._active_scopes and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
        if self._active_scopes:
            for scope in list(self._active_scopes.values()):
                scope.cancel("worker_drain_timeout")
            self._active_scopes.clear()

        # --- Stop engine-driven tasks (if running) ----
        if self._engine_bus_task is not None:
            self._engine_bus_task.cancel()
            try:
                await self._engine_bus_task
            except (asyncio.CancelledError, Exception):
                pass
            self._engine_bus_task = None
        if self._engine_claim_task is not None:
            self._engine_claim_task.cancel()
            try:
                await self._engine_claim_task
            except (asyncio.CancelledError, Exception):
                pass
            self._engine_claim_task = None

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
            "primer_session_turns_total": dict(self._turns_total_by_result),
            "primer_session_turn_duration_seconds": {
                "count": self._turn_duration_count,
                "sum": self._turn_duration_seconds_total,
            },
        }

    # ---- internal --------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(self.config.heartbeat_interval_seconds)
                if self._stopping.is_set():
                    return
                try:
                    await self._scheduler.heartbeat_worker(self._worker_id)
                    # Engine path: heartbeat all in-flight leases via engine.
                    if self._in_flight:
                        confirmed = await self._engine.heartbeat(
                            self._worker_id, list(self._in_flight),
                        )
                        confirmed_set = set(confirmed)
                        lost = self._in_flight - confirmed_set
                        for kind_id in lost:
                            scope = self._active_scopes.get(kind_id)
                            if scope is not None:
                                scope.cancel("preempted")
                except Exception:
                    logger.exception("heartbeat_loop iteration failed")
        except asyncio.CancelledError:
            return

    # ---- engine-driven loops (Task 13: one loop, one bus loop) -----------

    async def _engine_claim_loop(self) -> None:
        """Unified claim loop driven by ClaimEngine.claim_due.

        Replaces _claim_loop + _claim_chat_loop + _claim_harness_loop when
        an engine is injected. Claims any eligible lease (session, chat, or
        harness) and dispatches via self._dispatch[lease.kind].
        """
        assert self._engine is not None
        try:
            while not self._stopping.is_set():
                free = self.config.concurrency - len(self._in_flight)
                if free <= 0:
                    self._wake.clear()
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
                    )
                except Exception:
                    logger.exception("engine claim_loop iteration failed")
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue
                if not leases:
                    self._claims_empty_total += 1
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(
                            self._wake.wait(),
                            timeout=self.config.poll_interval_seconds,
                        )
                    except TimeoutError:
                        pass
                    continue
                self._claims_total += len(leases)
                # Reserve in_flight slots immediately before dispatching
                # so back-to-back claim iterations see the correct free count.
                for lease in leases:
                    self._in_flight.add((lease.kind, lease.entity_id))
                for lease in leases:
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
        except asyncio.CancelledError:
            return

    async def _run_engine(
        self,
        lease: ClaimLease,
        handler: Callable[[ClaimLease], Coroutine],
    ) -> None:
        """Wrapper that manages _in_flight bookkeeping + a cancel scope
        around a handler call. The scope lets the heartbeat loop preempt a
        running turn of ANY kind when its lease is lost."""
        key = (lease.kind, lease.entity_id)
        scope = _CancelScope()
        self._active_scopes[key] = scope
        try:
            async with scope:
                await handler(lease)
        except asyncio.CancelledError:
            logger.info(
                "engine handler for %s/%s cancelled (preempted)",
                lease.kind, lease.entity_id,
            )
        except Exception:
            logger.exception(
                "engine handler for %s/%s raised unexpectedly",
                lease.kind, lease.entity_id,
            )
        finally:
            self._active_scopes.pop(key, None)
            self._in_flight.discard(key)
            self._wake.set()

    async def _engine_bus_loop(self) -> None:
        """Subscribe to ClaimEngine.watch_ready and wake the claim loop."""
        assert self._engine is not None
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                async for _kind, _entity_id in self._engine.watch_ready():
                    self._wake.set()
                    if self._stopping.is_set():
                        return
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "engine_bus_loop watch_ready raised; restarting in %.1fs",
                    backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, 30.0)
            else:
                backoff = 1.0

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
                else:
                    outcome = await self._resume_engine_session(engine_lease, session_row)
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
                await self._engine.release(engine_lease, outcome=outcome)
            except Exception:
                logger.exception(
                    "_run_engine_session: engine.release for %s failed", sid,
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
        else:
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
        'resumable'), parked_state, and turn_no untouched, so a later /resume
        re-arms the lease and replays the hook."""
        from primer.int.claim import ReleaseOutcome

        storage = self._storage.get_storage(WorkspaceSession)
        fresh = await storage.get(session.id)
        if fresh is not None:
            paused = fresh.model_copy(update={"status": SessionStatus.PAUSED})
            await storage.update(paused)
        else:
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

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload):
        return await graph_resume_coordinator.graph_agent_tool_result(
            self, checkpoint, tcid, payload,
        )

    def _repark_graph_outcome(self, session, repark):
        return graph_resume_coordinator.repark_graph_outcome(self, session, repark)

    def _repark_resumed_yield_outcome(self, session, parked, yld):
        return session_resume_coordinator.repark_resumed_yield_outcome(
            self, session, parked, yld,
        )

    async def _run_engine_chat(self, engine_lease: ClaimLease) -> None:
        return await engine_handlers.run_engine_chat(self, engine_lease)

    async def _run_engine_harness(self, engine_lease: ClaimLease) -> None:
        return await engine_handlers.run_engine_harness(self, engine_lease)

    async def _run_engine_trigger(self, engine_lease: ClaimLease) -> None:
        return await engine_handlers.run_engine_trigger(self, engine_lease)

    async def _cancel_loop(self) -> None:
        """Drain cancel notifications. When a sid arrives that this worker
        holds an active scope for, fire scope.cancel(reason). The reason
        string is informational; the running turn inspects the Session row
        to determine cancel-vs-pause routing.

        Restart-on-failure pattern mirrors :meth:`_notify_loop`.
        """
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                cancel_iter = self._cancel_iter()
                async for sid in cancel_iter:
                    scope = self._active_scopes.get((ClaimKind.SESSION, sid))
                    if scope is not None:
                        scope.cancel("user_signal")
                    if self._stopping.is_set():
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

    async def _resolve_llm_model(self, agent):
        return await executor_builders.resolve_llm_model(self, agent)

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
