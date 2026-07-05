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
from primer.model.yield_ import YieldToWorker
from primer.worker.turn import _CancelScope
from primer.worker.yield_resume_registry import get_resume_hook
from primer.worker.yield_runtime import (
    _resume_tool_approval,
    classify_approval_payload,
    classify_resume_payload,
    ParkedState,
)

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


# Scoped tool ids are ``<toolset_id>__<tool_name>``; the worker only
# needs to resolve each unique toolset prefix to get the providers it
# has to load. Scoped ids without the separator are skipped silently —
# they can't reference a real tool anyway, and the agent definition
# is operator-owned so we don't want to 500 on a malformed entry.
def _toolset_ids_from_scoped(scoped_tool_ids: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}  # dict preserves insertion order
    for sid in scoped_tool_ids or []:
        if "__" not in sid:
            continue
        prefix = sid.rsplit("__", 1)[0]
        if prefix:
            seen.setdefault(prefix, None)
    return list(seen)


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

    async def _maybe_rearm_session(self, session_id: str) -> None:
        """Re-arm a fresh SESSION claim lease if a turn is still queued.

        Mirrors the CHAT lane's keep-the-lease-while-there's-more-work rule
        (``primer.worker.pool._run_engine_chat`` maps its turn's
        disposition to ``drop_lease``, and ``ChatClaimAdapter.on_release``
        is the single writer of the resulting ``turn_status``). Sessions
        can't reuse that exact shape because every session
        ``ReleaseOutcome`` drops the lease unconditionally (see
        ``primer.session.dispatch``), so instead of keeping the old lease
        alive this re-upserts a brand-new one, once release has actually
        dropped the old one -- calling upsert first would just touch the
        (still held-by-us, about to be dropped) lease's priority and be
        wiped out the instant release runs.

        ``run_one_session_turn`` consumes ("idle"s) any ``turn_status``
        it started with before running the turn, so a lingering
        ``turn_status == "claimable"`` at this point can only mean a
        ``wake_session()`` steer landed during (or right after) the turn
        that just released -- not a stale, already-serviced signal.

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
        """Persist the resolved approval decision for a session park.

        Best-effort: a write failure is logged and swallowed so the resume
        proceeds. Shared by the agent and graph resume paths via the same
        parked-state blob shape.
        """
        from primer.agent.approval_record import (
            record_from_parked_blob,
            write_approval_record,
        )
        from primer.model.tool_approval import ToolApprovalRecord

        decision, reason = classify_approval_payload(payload)
        record = record_from_parked_blob(
            blob=blob,
            decision=decision,
            reason=reason,
            agent_id=getattr(session.binding, "agent_id", None),
            session_id=session.id,
            requested_at=session.parked_at,
        )
        storage = (
            self._storage.get_storage(ToolApprovalRecord)
            if self._storage is not None
            else None
        )
        await write_approval_record(storage, record)

    async def _write_approval_record_for_graph(
        self, *, session, checkpoint: dict, tcid, payload,
    ) -> None:
        return await graph_resume_coordinator.write_approval_record_for_graph(
            self, session=session, checkpoint=checkpoint, tcid=tcid, payload=payload,
        )

    async def _resume_engine_session(self, engine_lease, session):
        """Drive a resumable park to its conclusion on the engine path.

        Engine-native resume dispatch: rehydrate the park, run the resume
        hook, inject the result, and return a ReleaseOutcome (no scheduler
        involvement):
          * agent success -> ReleaseOutcome(success=True, drop_lease=False):
            on_release clears the park columns + bumps turn_no, the lease is
            kept so the next claim runs the continuation LLM turn.
          * fail-closed   -> _end_session(reason='failed') (drop_lease=True).
        """
        import json
        from primer.model.chat import ToolResultPart

        sid = session.id
        blob = session.parked_state or {}
        try:
            parked = ParkedState.from_jsonable(blob)
        except (KeyError, ValueError, TypeError):
            logger.exception(
                "resume: malformed parked_state for session %s - ending failed",
                sid,
            )
            return await self._end_session(session, reason="failed")

        if session.binding.kind == "graph":
            if parked.graph_checkpoint is None:
                logger.error(
                    "resume: graph session %s parked without a graph_checkpoint"
                    " - ending failed", sid,
                )
                return await self._end_session(session, reason="failed")
            return await self._resume_graph_engine(session, parked)

        if session.binding.kind != "agent":
            logger.error(
                "resume: unsupported binding kind %r for session %s - ending"
                " failed", session.binding.kind, sid,
            )
            return await self._end_session(session, reason="failed")

        if session.parked_at is None:
            logger.error(
                "resume: session %s resumable but parked_at=None - ending failed",
                sid,
            )
            return await self._end_session(session, reason="failed")

        resume_payload = classify_resume_payload(parked, parked_at=session.parked_at)

        workspace = await self._load_workspace_for_persist(session.workspace_id)
        executor_or_driver = await self._build_agent_executor(session, workspace)
        executor = getattr(executor_or_driver, "_executor", executor_or_driver)
        tool_manager = getattr(executor, "_tool_manager", None)

        # Unified nested-yield continuation: a non-empty frame stack means the
        # leaf yield was raised INSIDE a nested invoke_agent invocation (the
        # session's own turn is NOT a frame - it lives in ``parked.llm_messages``
        # and is resumed by the shared inject tail below). Walk the frames to
        # resolve the leaf and unwind the chain into a single tool_result, then
        # fall through to the SAME inject/continue tail the per-tool_name path
        # uses. An empty stack routes to the existing switch UNCHANGED, which
        # preserves the persist-approvals decision-record writes and the
        # invoke_graph regression until task 5.1 migrates them.
        if parked.frames:
            from primer.worker.continuation import Repark, resume_continuation

            services = self._build_invocation_services(
                session, workspace, executor, tool_manager,
            )
            try:
                outcome = await resume_continuation(
                    parked.frames,
                    parked.yielded,
                    resume_payload.payload,
                    services,
                )
            except Exception as exc:  # noqa: BLE001 - fail-closed synthesis
                logger.exception(
                    "resume: continuation walk for session %s raised;"
                    " synthesising error tool_result", sid,
                )
                tool_result_part = ToolResultPart(
                    id=parked.tool_call_id or "unknown",
                    output=json.dumps({
                        "rejected": True,
                        "reason": (
                            f"continuation resume failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        "tool_name": parked.yielded.tool_name,
                    }),
                    error=True,
                )
            else:
                if isinstance(outcome, Repark):
                    # A frame (or the leaf re-dispatch) raised a fresh yield
                    # mid-unwind -> re-park the reconstructed stack + new leaf.
                    return self._repark_continuation(session, parked, outcome)
                tool_result_part = outcome.tool_result
            return await self._inject_resume_and_continue(
                session, executor, parked, tool_result_part,
            )

        tool_name = parked.yielded.tool_name
        try:
            if tool_name == "_approval":
                # Persist the resolved decision (approved/rejected/timeout/
                # cancelled) exactly once, BEFORE we re-dispatch/synthesise.
                # classify_approval_payload is the same classifier the resume
                # uses, so the record's verdict cannot drift from the result.
                await self._write_approval_record_for_session(
                    session=session, blob=blob, payload=resume_payload.payload,
                )
                tool_result_part = await _resume_tool_approval(
                    blob=blob,
                    payload=resume_payload.payload,
                    tool_manager=tool_manager,
                )
            else:
                hook = get_resume_hook(tool_name)
                hook_result = hook(parked.yielded.resume_metadata, resume_payload.payload)
                if asyncio.iscoroutine(hook_result):
                    hook_result = await hook_result
                tool_result_part = ToolResultPart(
                    id=parked.tool_call_id or "unknown",
                    output=hook_result.output,
                    error=hook_result.is_error,
                )
        except YieldToWorker as yld:
            # Two-phase park: an approval gate sat on a *yielding* tool. The
            # operator just APPROVED (phase 1), so _resume_tool_approval
            # re-dispatched the real tool with bypass_approval=True - which
            # itself yields for its own event (timer/file/graph/human). Do
            # NOT swallow this as an error: re-park the session on the new
            # event key (phase 2), preserving the in-progress turn messages,
            # so it resumes when the real event fires. Mirrors the normal
            # park path in primer/session/dispatch.py and
            # _repark_continuation below.
            return self._repark_resumed_yield_outcome(session, parked, yld)
        except Exception as exc:  # noqa: BLE001 - fail-closed synthesis
            logger.exception(
                "resume: hook for tool %r on session %s raised; synthesising"
                " error tool_result", tool_name, sid,
            )
            tool_result_part = ToolResultPart(
                id=parked.tool_call_id or "unknown",
                output=json.dumps({
                    "rejected": True,
                    "reason": f"resume failed: {type(exc).__name__}: {exc}",
                    "tool_name": tool_name,
                }),
                error=True,
            )

        return await self._inject_resume_and_continue(
            session, executor, parked, tool_result_part,
        )

    async def _inject_resume_and_continue(
        self, session, executor, parked, tool_result_part,
    ):
        """Inject the resolved tool_result into the parked turn + continue.

        Shared tail for BOTH the per-tool_name resume switch and the new
        nested-yield continuation walk: rehydrate the parked turn's assistant
        history, append the resolved ``tool_result_part`` as a tool message,
        persist them via ``inject_resume_messages``, and return the
        keep-the-lease continuation outcome (the next claim runs the
        continuation LLM turn). On persist failure it fails the session.
        """
        from primer.int.claim import ReleaseOutcome
        from primer.model.chat import Message

        rehydrated_assistant = [Message.model_validate(m) for m in parked.llm_messages]
        tool_result_msg = Message(role="tool", parts=[tool_result_part])
        try:
            await executor.inject_resume_messages(
                [*rehydrated_assistant, tool_result_msg],
            )
        except Exception:
            logger.exception(
                "resume: persist failed for session %s - ending failed",
                session.id,
            )
            return await self._end_session(session, reason="failed")

        # Continuation: clear park (on_release) + keep the lease so the next
        # claim runs the continuation LLM turn.
        return ReleaseOutcome(success=True, drop_lease=False)

    def _build_invocation_services(self, session, workspace, executor, tool_manager):
        """Build the :class:`InvocationServices` bundle the continuation walk
        drives nested invocations through.

        Binds the worker's storage / provider-registry / approval-resolver into
        thin closures over :func:`primer.agent.invoke.build_subagent_toolmanager`
        and :func:`primer.agent.invoke.resume_subagent` (the same deps the worker
        wires a normal turn with), and threads the session's
        :class:`GraphInvocationServices` (off the tool_manager) for the
        graph-frame callables.
        The walk only ever calls these as ``services.<name>(...)``.
        """
        from primer.agent.invoke import (
            build_subagent_toolmanager as _build_subagent_toolmanager,
            resume_subagent as _resume_subagent,
        )
        from primer.worker.continuation import InvocationServices

        storage_provider = self._storage
        provider_registry = self._provider_registry
        approval_resolver = self._approval_resolver

        async def build_subagent_toolmanager(context):
            return await _build_subagent_toolmanager(
                context,
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                approval_resolver=approval_resolver,
            )

        async def resume_subagent(
            *, agent_id, context, llm_messages, child_result, depth,
            invoke_tool_call_id,
        ):
            return await _resume_subagent(
                agent_id=agent_id,
                context=context,
                llm_messages=llm_messages,
                child_result=child_result,
                depth=depth,
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                approval_resolver=approval_resolver,
                invoke_tool_call_id=invoke_tool_call_id,
            )

        # Graph callables come off the session's GraphInvocationServices, which
        # the agent's tool_manager carries (set by _build_agent_executor); the
        # GraphFrame path that uses them only lands once task 5.1 migrates
        # invoke_graph onto the continuation walk. Bind defensively so an
        # absent bundle yields a clear error rather than an AttributeError.
        graph_services = getattr(tool_manager, "_graph_services", None)

        async def resolve_graph(graph_id):
            if graph_services is None:
                raise RuntimeError("graph services unavailable for this session")
            return await graph_services.resolve_graph(graph_id)

        async def build_child_graph_executor(graph, gsid):
            if graph_services is None:
                raise RuntimeError("graph services unavailable for this session")
            return await graph_services.build_child_executor(graph=graph, gsid=gsid)

        async def graph_agent_tool_result(checkpoint, tcid, payload):
            # Reuse the worker's own helper so a GraphFrame leaf resolves an
            # agent-node ask_user answer consistently.
            return await self._graph_agent_tool_result(checkpoint, tcid, payload)

        return InvocationServices(
            build_subagent_toolmanager=build_subagent_toolmanager,
            resume_subagent=resume_subagent,
            resolve_graph=resolve_graph,
            build_child_graph_executor=build_child_graph_executor,
            graph_agent_tool_result=graph_agent_tool_result,
        )

    def _repark_continuation(self, session, parked, outcome):
        """Re-park an AGENT session whose nested continuation re-yielded.

        A frame's resume (or the leaf re-dispatch) raised a fresh yield
        mid-unwind: the continuation walk returns a :class:`Repark` carrying the
        reconstructed (root-first) frame stack + the new innermost leaf. Persist
        a fresh :class:`ParkedState` whose ``frames`` is the reconstructed stack
        and whose ``yielded`` is the new leaf, preserving the SESSION turn's
        ``llm_messages`` + ``tool_call_id`` so the eventual completion pairs
        correctly.
        """
        from datetime import timedelta
        from primer.int.claim import ParkRequest, ReleaseOutcome

        leaf = outcome.leaf
        now = datetime.now(timezone.utc)
        timeout = leaf.timeout if leaf.timeout is not None else 3600.0
        new_parked = ParkedState(
            yielded=leaf,
            llm_messages=parked.llm_messages,
            turn_no=session.turn_no,
            started_at=now,
            tool_call_id=parked.tool_call_id,
            frames=list(outcome.frames),
        )
        return ReleaseOutcome(
            success=True,
            drop_lease=True,
            park=ParkRequest(
                parked_state=new_parked.to_jsonable(),
                parked_event_key=leaf.event_key,
                parked_event_keys=getattr(leaf, "event_keys", None),
                parked_until=now + timedelta(seconds=timeout),
                parked_at=now,
            ),
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
        """Re-park an AGENT session whose approval-gated tool, once approved,
        yielded for its OWN real event (phase 2 of the two-phase park).

        Builds a fresh ParkedState from the re-raised YieldToWorker's event
        key / tool_call_id / resume_metadata, preserving the in-progress turn's
        rehydrated assistant messages so the eventual real-event resume pairs
        the tool_result against the original tool_use. Mirrors the normal park
        path in primer/session/dispatch.py and _repark_continuation.
        """
        from datetime import timedelta
        from primer.int.claim import ParkRequest, ReleaseOutcome

        yielded = yld.yielded
        now = datetime.now(timezone.utc)
        timeout = yielded.timeout if yielded.timeout is not None else 3600.0

        # Stamp parked_at_iso so the eventual resume hook can compute elapsed
        # without a separate read (mirrors dispatch.py's first-park path).
        resume_metadata = dict(yielded.resume_metadata)
        resume_metadata["parked_at_iso"] = now.isoformat()
        yielded_stamped = type(yielded)(
            tool_name=yielded.tool_name,
            event_key=yielded.event_key,
            timeout=yielded.timeout,
            resume_metadata=resume_metadata,
            event_keys=getattr(yielded, "event_keys", None),
        )

        new_parked = ParkedState(
            yielded=yielded_stamped,
            # Preserve the in-progress turn history (the assistant message that
            # emitted the original tool_use) so the real-event resume can pair
            # the synthesised tool_result against it.
            llm_messages=parked.llm_messages,
            turn_no=session.turn_no,
            started_at=now,
            tool_call_id=yld.tool_call_id,
            graph_checkpoint=getattr(yld, "graph_checkpoint", None),
        )
        return ReleaseOutcome(
            success=True,
            drop_lease=True,
            park=ParkRequest(
                parked_state=new_parked.to_jsonable(),
                parked_event_key=yielded_stamped.event_key,
                parked_event_keys=getattr(yielded_stamped, "event_keys", None),
                parked_until=now + timedelta(seconds=timeout),
                parked_at=now,
            ),
        )

    async def _run_engine_chat(self, engine_lease: ClaimLease) -> None:
        """Handle a CHAT claim from the engine.

        Bridges to run_one_chat_turn via ChatDispatchDeps.
        Atomically stamps claimed_by + turn_status='running' on the chat row
        before dispatching, then releases the engine lease on completion.
        """
        from primer.chat.dispatch import ChatDispatchDeps, run_one_chat_turn
        from primer.int.claim import ReleaseOutcome
        from primer.model.chats import Chat

        assert self._event_bus is not None, (
            "WorkerPool._run_engine_chat requires an event_bus"
        )
        assert self._chat_tick_router is not None, (
            "WorkerPool._run_engine_chat requires a chat_tick_router"
        )

        # Transition turn_status to 'running' before dispatching. 'running'
        # is accepted here for crash recovery: the claim engine only hands us
        # a 'running' chat when its prior worker's lease expired (died/stalled
        # past the TTL), so we re-run the interrupted turn. Fencing
        # (attempt_id / lease loss) stops the dead worker from double-writing.
        chat_storage = self._storage.get_storage(Chat)
        chat = await chat_storage.get(engine_lease.entity_id)
        if chat is None or chat.turn_status not in (
            "claimable", "running",
        ):
            await self._engine.release(
                engine_lease,
                outcome=ReleaseOutcome(success=False, drop_lease=True),
            )
            return
        await chat_storage.update(chat.model_copy(update={
            "turn_status": "running",
        }))

        chat_channel_dispatcher = None
        if self._channel_dispatcher is not None:
            from primer.channel.chat_dispatcher import ChatChannelDispatcher
            chat_channel_dispatcher = ChatChannelDispatcher(
                storage_provider=self._storage,
                registry=self._channel_dispatcher._registry,
                # Out-of-proc workers never warm inbound gateways, so relay
                # peeks the (cold) registry and falls back to the bus; the
                # API process fulfils the post. In-proc (api+worker) the
                # shared registry is warm and relay posts directly.
                event_bus=self._event_bus,
                artifact_registry=self._artifact_storage_registry,
            )

        deps = ChatDispatchDeps(
            storage_provider=self._storage,
            provider_registry=self._provider_registry,
            event_bus=self._event_bus,
            chat_tick_router=self._chat_tick_router,
            chat_channel_dispatcher=chat_channel_dispatcher,
            artifact_storage_registry=self._artifact_storage_registry,
        )
        # run_one_chat_turn returns the terminal turn_status DISPOSITION;
        # it no longer writes turn_status itself. We map it to the
        # ReleaseOutcome the fenced ChatClaimAdapter.on_release turns into
        # the terminal turn_status (the single writer). Adapter rule:
        # idle iff (success and drop_lease). So 'idle' -> drop the lease
        # (idle); 'claimable' -> keep success but DON'T drop the lease so
        # the adapter computes 'claimable' (re-served). A raised turn is
        # treated as 'claimable' too.
        disposition = "claimable"
        try:
            disposition = await run_one_chat_turn(
                deps,
                chat_id=engine_lease.entity_id,
                worker_id=self._worker_id,
            )
        except Exception:
            logger.exception(
                "engine chat turn for %s raised",
                engine_lease.entity_id,
            )
        finally:
            drop_lease = disposition == "idle"
            await self._engine.release(
                engine_lease,
                outcome=ReleaseOutcome(success=True, drop_lease=drop_lease),
            )

    async def _run_engine_harness(self, engine_lease: ClaimLease) -> None:
        """Handle a HARNESS claim from the engine.

        Bridges to run_one_harness_operation via HarnessDispatchDeps.
        Stamps claimed_by on the harness row so heartbeat checks pass during
        long operations.
        """
        from primer.harness.dispatch import HarnessDispatchDeps, run_one_harness_operation
        from primer.int.claim import ReleaseOutcome
        from primer.model.harness import Harness

        # Verify the harness still has a pending operation before dispatching.
        harness_storage = self._storage.get_storage(Harness)
        harness = await harness_storage.get(engine_lease.entity_id)
        if harness is None or harness.pending_operation is None:
            await self._engine.release(
                engine_lease,
                outcome=ReleaseOutcome(success=False, drop_lease=True),
            )
            return

        deps = HarnessDispatchDeps(
            storage_provider=self._storage,
            event_bus=self._event_bus,
            provider_registry=self._provider_registry,
            semantic_search_registry=self._semantic_search_registry,
        )
        success = False
        try:
            await run_one_harness_operation(
                deps,
                harness_id=engine_lease.entity_id,
                worker_id=self._worker_id,
            )
            success = True
        except Exception:
            logger.exception(
                "engine harness operation for %s raised",
                engine_lease.entity_id,
            )
        finally:
            await self._engine.release(
                engine_lease,
                outcome=ReleaseOutcome(success=success, drop_lease=True),
            )

    async def _run_engine_trigger(self, engine_lease: ClaimLease) -> None:
        """Handle a TRIGGER claim from the engine.

        Routes the lease to :func:`primer.trigger.dispatch.fire_trigger`,
        which fans out to each enabled subscription's dispatcher. The
        ``TriggerClaimAdapter.on_release`` hook advances ``next_fire_at``
        (cron tick for ``scheduled``, null/disabled for ``delayed``) so
        the engine's next claim window is correct.

        Catchup handling (spec §8): when the trigger's ``catchup`` is
        ``'all'`` and the row has a ``last_fired_at``, enumerate every
        missed cron tick between then and now (bounded to 64 to avoid
        runaway) and fire each one with the historical ``scheduled_for``
        instant. After replaying the backlog we fire the current tick
        with ``scheduled_for=None``. ``'one'`` and ``'none'`` (and all
        non-scheduled kinds) fire exactly once with ``scheduled_for=None``.
        """
        from datetime import datetime, timezone

        from primer.int.claim import ReleaseOutcome
        from primer.model.trigger import Trigger
        from primer.trigger.cron import iter_missed_fires
        from primer.trigger.dispatch import fire_trigger
        from primer.trigger.subscribers import DispatchDeps

        deps = DispatchDeps(
            storage_provider=self._storage,
            claim_engine=self._engine,
            scheduler=self._scheduler,
            workspace_registry=getattr(self, "_workspace_registry", None),
            event_bus=self._event_bus,
        )

        success = False
        try:
            # Catchup replay for scheduled triggers with catchup='all'.
            # Best-effort: any failure in the backlog walk falls through
            # to the current-tick fire so a malformed cron / tz doesn't
            # silently block normal firing. The current tick's own
            # errors are still raised to the outer except.
            triggers_storage = self._storage.get_storage(Trigger)
            trigger = await triggers_storage.get(engine_lease.entity_id)
            if (
                trigger is not None
                and trigger.enabled
                and trigger.config.kind == "scheduled"
                and getattr(trigger.config, "catchup", "one") == "all"
                and trigger.last_fired_at is not None
            ):
                now = datetime.now(timezone.utc)
                try:
                    missed = list(iter_missed_fires(
                        trigger.config.cron,
                        trigger.config.timezone,
                        from_=trigger.last_fired_at,
                        now=now,
                        limit=64,
                    ))
                except Exception:
                    logger.exception(
                        "trigger %s: catchup enumeration failed; "
                        "continuing to current-tick fire",
                        engine_lease.entity_id,
                    )
                    missed = []
                for missed_ts in missed:
                    try:
                        await fire_trigger(
                            trigger_id=engine_lease.entity_id,
                            scheduled_for=missed_ts,
                            deps=deps,
                        )
                    except Exception:
                        logger.exception(
                            "trigger %s: catchup fire at %s raised; "
                            "skipping to next",
                            engine_lease.entity_id, missed_ts.isoformat(),
                        )

            await fire_trigger(
                trigger_id=engine_lease.entity_id,
                scheduled_for=None,
                deps=deps,
            )
            success = True
        except Exception:
            logger.exception(
                "engine trigger fire for %s raised",
                engine_lease.entity_id,
            )
        finally:
            await self._engine.release(
                engine_lease,
                outcome=ReleaseOutcome(success=success, drop_lease=False),
            )

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
        self, *, workspace, workspace_session, graph_session_id: str,
    ):
        return executor_builders.build_graph_invocation_services(
            self,
            workspace=workspace,
            workspace_session=workspace_session,
            graph_session_id=graph_session_id,
        )

    async def _build_agent_executor(self, session: WorkspaceSession, workspace):
        return await executor_builders.build_agent_executor(self, session, workspace)

    async def _build_graph_executor(self, session: WorkspaceSession, workspace):
        return await executor_builders.build_graph_executor(self, session, workspace)

    async def _resolve_llm_model(self, agent):
        return await executor_builders.resolve_llm_model(self, agent)

    def _infer_post_turn_status(self, executor, session: WorkspaceSession) -> SessionStatus:
        return executor_builders.infer_post_turn_status(self, executor, session)


class _TurnDriver:
    """Adapter that consumes a streaming executor for the turn-based pool.

    The workspace executors expose ``invoke`` as an async generator
    (yielding :class:`StreamEvent`s), but the pool dispatches turns via
    ``await executor.invoke([])`` -- a single-shot coroutine. This adapter
    bridges the two: drain the generator to completion inside an awaitable
    ``invoke`` and surface :attr:`last_done_reason` from the underlying
    executor for the post-turn status mapper to read.
    """

    def __init__(self, executor) -> None:
        self._executor = executor

    @property
    def last_done_reason(self) -> str | None:
        return getattr(self._executor, "last_done_reason", None)

    @property
    def session(self):
        # Pass-through to support tests / introspection that want the
        # underlying :class:`AgentSession`.
        return getattr(self._executor, "session", None)

    async def invoke(self, messages, *, response_format=None) -> None:
        """Drain the executor's stream to completion.

        Events are intentionally discarded here -- streaming-tap
        subscribers attached via :meth:`_BaseAgentExecutor.subscribe`
        still receive them via the executor's own fan-out.
        """
        async for _ev in self._executor.invoke(
            messages, response_format=response_format
        ):
            pass


class _GraphTurnDriver:
    """Adapter for :class:`primer.graph.workspace_executor.WorkspaceGraphExecutor`.

    Two differences from :class:`_TurnDriver`:

    * Graph executor's ``invoke(messages)`` does NOT accept a
      ``response_format`` kwarg (per-node response_format lives on
      each agent node), so this adapter discards the kwarg the worker
      passes uniformly.
    * The graph executor runs the WHOLE graph in one ``invoke()`` call
      (multiple supersteps complete internally before returning), so
      ``last_done_reason`` is a fixed ``"graph_ended"`` sentinel that
      :meth:`WorkerPool._infer_post_turn_status` recognises as ENDED
      — the session is never re-enqueued.
    """

    def __init__(self, executor) -> None:
        self._executor = executor

    @property
    def last_done_reason(self) -> str:
        return "graph_ended"

    @property
    def session(self):
        # Graphs have no single AgentSession in Phase 1; expose None
        # so the pool's introspection callers see a consistent shape.
        return getattr(self._executor, "_workspace_session", None)

    async def invoke(self, messages, *, response_format=None) -> None:
        """Drain the graph executor's stream to completion.

        ``response_format`` is accepted for signature compatibility
        with :class:`_TurnDriver` and silently discarded — graph nodes
        carry their own per-node ``response_format`` on the
        :class:`_AgentNodeRef` model.
        """
        async for _ev in self._executor.invoke(messages):
            pass


class _WorkspaceIOShim:
    """``WorkspaceIO`` adapter that delegates to the workspace runtime.

    Satisfies the :class:`primer.session.persistence.WorkspaceIO` protocol
    used by :class:`WorkspaceMessageWriter`.

    Dispatch: resolves the workspace from the registry and calls
    ``workspace.append_message_line(session_id, line)`` directly.  Every
    concrete :class:`primer.int.workspace.Workspace` backend now implements
    this method (added in Task 9).

    The shim calls ``_workspace_registry.get_workspace(workspace_id)`` on
    each flush so hot-reloaded workspace instances are picked up during
    long-lived workers.  Because the session_id alone is enough to identify
    the session slot inside the workspace, but the workspace_id is needed to
    locate the workspace, the shim tracks the mapping via a lightweight
    ``_session_to_workspace`` dict populated via :meth:`register_session`
    before the first write.
    """

    def __init__(self, workspace_registry) -> None:
        self._registry = workspace_registry
        # session_id -> workspace_id mapping; populated via register_session()
        # from the _build_session_executor path before any append is called.
        self._session_to_workspace: dict[str, str] = {}

    def register_session(self, session_id: str, workspace_id: str) -> None:
        """Pre-register the workspace_id for a session (called by the pool)."""
        self._session_to_workspace[session_id] = workspace_id

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        """Append ``line`` to the session's ``messages.jsonl`` via the workspace runtime."""
        if self._registry is None:
            logger.warning(
                "_WorkspaceIOShim: no workspace_registry configured; "
                "dropping %d bytes for session %s",
                len(line), session_id,
            )
            return

        workspace_id = self._session_to_workspace.get(session_id)
        if workspace_id is None:
            logger.warning(
                "_WorkspaceIOShim: no workspace_id registered for session %s; "
                "dropping %d bytes",
                session_id, len(line),
            )
            return

        workspace = await self._registry.get_workspace(workspace_id)
        if workspace is None:
            logger.warning(
                "_WorkspaceIOShim: workspace %r not found for session %s; "
                "dropping %d bytes",
                workspace_id, session_id, len(line),
            )
            return

        await workspace.append_message_line(session_id, line)

    def workspace_id_for(self, session_id: str) -> str | None:
        """Public lookup for the workspace id bound to a session.

        Replaces direct reads of the private ``_session_to_workspace``
        dict by call sites that need to resolve a session's workspace
        (e.g. dispatch's turn-log factory closure).
        """
        return self._session_to_workspace.get(session_id)

    async def append_state_line(
        self, workspace_id: str, state_relative_path: str, line: bytes,
    ) -> None:
        """Append ``line`` to ``<workspace.state_path>/<state_relative_path>``.

        Resolves the workspace via the registry, prepends the workspace's
        own ``state_path`` (so operators can override the default
        ``.state`` via :class:`WorkspaceTemplate` without losing the
        writer/reader path agreement), then delegates to the backend's
        ``append_state_line``. Logs and drops the bytes if the registry
        is absent or the workspace can't be resolved (mirroring
        ``append_message_line``'s best-effort policy).
        """
        if self._registry is None:
            logger.warning(
                "_WorkspaceIOShim: no workspace_registry configured; "
                "dropping %d state bytes for workspace %s",
                len(line), workspace_id,
            )
            return
        workspace = await self._registry.get_workspace(workspace_id)
        if workspace is None:
            logger.warning(
                "_WorkspaceIOShim: workspace %r not found; "
                "dropping %d state bytes",
                workspace_id, len(line),
            )
            return
        state_path = getattr(workspace, "state_path", ".state")
        full_path = f"{state_path}/{state_relative_path}"
        try:
            await workspace.append_state_line(full_path, line)
        except NotImplementedError:
            # Backend without turn-log support; silently no-op so
            # the dispatch doesn't bubble the failure.
            logger.debug(
                "_WorkspaceIOShim: workspace %r has no append_state_line; "
                "dropping %d bytes", workspace_id, len(line),
            )

    async def read_state_file(
        self, workspace_id: str, state_relative_path: str,
    ) -> bytes:
        """Read ``<workspace.state_path>/<state_relative_path>`` from the workspace.

        Returns ``b""`` when the workspace is gone, the path doesn't
        exist, or any other backend error fires. Used by the turn-log
        writer's lazy bootstrap so the same path-resolution rule
        applies to both reads and writes.
        """
        if self._registry is None:
            return b""
        workspace = await self._registry.get_workspace(workspace_id)
        if workspace is None:
            return b""
        state_path = getattr(workspace, "state_path", ".state")
        full_path = f"{state_path}/{state_relative_path}"
        try:
            return await workspace.read_file(full_path)
        except Exception:  # noqa: BLE001
            return b""


# Imported at the bottom so the helper modules (which import names defined
# above, e.g. ``_toolset_ids_from_scoped`` / ``_TurnDriver`` / ``WorkerPool``)
# resolve against a fully-initialised ``primer.worker.pool`` module and the
# import cycle never bites. The WorkerPool delegators reference these by
# attribute at call time.
from primer.worker import executor_builders  # noqa: E402
from primer.worker import graph_resume_coordinator  # noqa: E402
