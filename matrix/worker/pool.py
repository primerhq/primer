"""Background worker pool — claims sessions and runs one turn each.

This module ships in three slices:

* Task 15 (this task) — skeleton: ``start()``, ``drain_and_stop()``,
  worker registration, and the ``_heartbeat_loop`` task.
* Task 16 — adds ``_claim_loop`` and ``_run_one_turn`` (happy path).
* Task 17 — adds the failure handlers (transient/fatal/cancel).

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
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from matrix.int.scheduler import (
    CompleteTurnResult,
    FailureRecord,
    Lease,
    Scheduler,
)
from matrix.model.except_ import TransientError
from matrix.model.scheduler import WorkerConfig
from matrix.model.session import Session, SessionStatus
from matrix.worker.turn import _CancelScope, compute_backoff

if TYPE_CHECKING:
    from matrix.api.registries import ProviderRegistry, WorkspaceRegistry
    from matrix.int.storage_provider import StorageProvider

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
    ) -> None:
        self.config = config
        self._scheduler = scheduler
        self._storage = storage
        self._workspace_registry = workspace_registry
        self._provider_registry = provider_registry

        self._worker_id: str = ""
        self._tasks: list[asyncio.Task] = []
        self._active_scopes: dict[str, _CancelScope] = {}
        self._in_flight: set[str] = set()
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
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
        # All four spec §6.2 loops are spawned here: heartbeat keeps the
        # worker row + lease TTLs alive, claim picks up runnable sessions,
        # notify wakes the claim loop on scheduler hints, and cancel fans
        # cancel signals into the active scope registry.
        self._tasks = [
            asyncio.create_task(
                self._heartbeat_loop(),
                name=f"scheduler-heartbeat-{self._worker_id}",
            ),
            asyncio.create_task(
                self._claim_loop(),
                name=f"scheduler-claim-{self._worker_id}",
            ),
            asyncio.create_task(
                self._notify_loop(),
                name=f"scheduler-notify-{self._worker_id}",
            ),
            asyncio.create_task(
                self._cancel_loop(),
                name=f"scheduler-cancel-{self._worker_id}",
            ),
        ]

    async def drain_and_stop(self) -> None:
        self._stopping.set()
        try:
            await self._scheduler.drain_worker(self._worker_id)
        except Exception:
            logger.exception("drain_worker failed for %s", self._worker_id)
        deadline = asyncio.get_event_loop().time() + self.config.drain_timeout_seconds
        while self._active_scopes and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
        if self._active_scopes:
            for scope in list(self._active_scopes.values()):
                scope.cancel("worker_drain_timeout")
            self._active_scopes.clear()
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
        function. Assumes ``session_id`` has been enqueued and is ready to
        claim. Raises if no lease is returned (the session wasn't actually
        runnable).
        """
        leases = await self._scheduler.claim(self._worker_id, max_count=1)
        matching = [l for l in leases if l.session_id == session_id]
        if not matching:
            raise RuntimeError(
                f"no runnable lease for session {session_id!r}; "
                "did you call scheduler.enqueue first?"
            )
        await self._run_one_turn(matching[0])

    # ---- Metrics ---------------------------------------------------------

    def metrics_snapshot(self) -> dict[str, Any]:
        """Snapshot of worker-pool metrics. See spec §14.

        Synchronous + lock-free: weak consistency is acceptable per
        spec §3 — concurrent claim/complete activity may race the
        snapshot but the values are still useful for dashboards.
        Histograms beyond ``count`` + ``sum`` are deferred (a real
        Prometheus exporter can fold these into proper buckets later)."""
        return {
            "matrix_worker_id": self._worker_id,
            "matrix_worker_in_flight": len(self._in_flight),
            "matrix_worker_capacity": self.config.concurrency,
            "matrix_worker_claims_total": self._claims_total,
            "matrix_worker_claims_empty_total": self._claims_empty_total,
            "matrix_session_turns_total": dict(self._turns_total_by_result),
            "matrix_session_turn_duration_seconds": {
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
                    if self._in_flight:
                        owned = await self._scheduler.heartbeat_leases(
                            self._worker_id, list(self._in_flight),
                        )
                        lost = self._in_flight - set(owned)
                        for sid in lost:
                            scope = self._active_scopes.get(sid)
                            if scope is not None:
                                scope.cancel("preempted")
                except Exception:
                    logger.exception("heartbeat_loop iteration failed")
        except asyncio.CancelledError:
            return

    async def _claim_loop(self) -> None:
        """Claim runnable sessions and dispatch them to _run_one_turn tasks.

        Each iteration:
        - if free capacity: claim up to (capacity - in_flight) sessions
        - for each lease, spawn a per-turn task (don't block the claim loop)
        - if claim returns empty: wait for the wake event with a poll
          timeout; the wake event fires on notify or on per-turn completion
        """
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
                    except asyncio.TimeoutError:
                        pass
                    continue
                try:
                    leases = await self._scheduler.claim(
                        self._worker_id,
                        max_count=min(self.config.claim_batch_size, free),
                    )
                except Exception:
                    logger.exception("claim_loop iteration failed")
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
                    except asyncio.TimeoutError:
                        pass
                    continue
                self._claims_total += len(leases)
                for lease in leases:
                    # Fire-and-forget: _run_one_turn manages its own
                    # in_flight bookkeeping via the finally block.
                    asyncio.create_task(
                        self._run_one_turn(lease),
                        name=f"turn-{lease.session_id}",
                    )
        except asyncio.CancelledError:
            return

    async def _notify_loop(self) -> None:
        """Drain ready notifications from the scheduler. Each one wakes the
        claim loop so it can attempt a fresh claim sooner than the poll."""
        try:
            async for _sid in self._scheduler.watch_ready(self._worker_id):
                self._wake.set()
                if self._stopping.is_set():
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("notify_loop terminated unexpectedly")

    async def _cancel_loop(self) -> None:
        """Drain cancel notifications. When a sid arrives that this worker
        holds an active scope for, fire scope.cancel(reason). The reason
        string is informational; the worker re-loads the Session row inside
        _handle_cancel to determine cancel-vs-pause routing."""
        try:
            cancel_iter = self._cancel_iter()
            async for sid in cancel_iter:
                scope = self._active_scopes.get(sid)
                if scope is not None:
                    scope.cancel("user_signal")
                if self._stopping.is_set():
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("cancel_loop terminated unexpectedly")

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

    async def _load_session(self, sid: str) -> Session | None:
        """Fetch the persisted Session row. Override in tests via monkeypatch."""
        sp_storage = self._storage.get_storage(Session)
        return await sp_storage.get(sid)

    async def _load_workspace_for_persist(self, workspace_id: str):
        """Fetch the live workspace handle for the current turn. Override in tests.

        Name kept for back-compat with test monkeypatches; this method is
        no longer tied to ``persist_turn`` (removed).
        """
        return await self._workspace_registry.get_workspace(workspace_id)

    async def _build_executor(self, session: Session, workspace):
        """Construct an executor for ``session`` against ``workspace``.

        Dispatches on ``session.binding.kind``:

        * ``'agent'``  -> :class:`WorkspaceAgentExecutor` driving the
          on-disk :class:`AgentSession` allocated at create time.
        * ``'graph'``  -> :class:`WorkspaceGraphExecutor` (deferred —
          see :meth:`_build_graph_executor`).

        Imports happen lazily inside the per-kind branch so this module
        doesn't pull executor + LLM dependencies at startup.
        """
        if session.binding.kind == "agent":
            return await self._build_agent_executor(session, workspace)
        if session.binding.kind == "graph":
            return await self._build_graph_executor(session, workspace)
        raise ValueError(
            f"unknown session binding kind: {session.binding.kind!r}"
        )

    async def _build_agent_executor(self, session: Session, workspace):
        """Build a turn-driver around :class:`WorkspaceAgentExecutor`.

        Resolves the agent definition (snapshot first, falls back to
        storage), the LLM via the provider registry, every toolset the
        agent registered, and the on-disk :class:`AgentSession` slot
        the API allocated at create time (id = ``session.id``).

        Returns a small adapter (not the executor itself) that exposes
        an awaitable ``invoke(messages)`` and a ``last_done_reason``
        attribute. The adapter consumes the executor's async-generator
        ``invoke`` to completion, since the worker's ``_run_one_turn``
        contract is ``await executor.invoke([])`` rather than
        iteration.
        """
        from matrix.agent.tool_manager import ToolExecutionManager
        from matrix.agent.workspace_executor import WorkspaceAgentExecutor
        from matrix.model.agent import Agent
        from matrix.model.except_ import NotFoundError

        binding = session.binding  # AgentSessionBinding
        # Resolve the Agent: prefer the snapshot if the API froze one
        # at create time, otherwise look up the live row.
        agent = binding.agent_snapshot
        if agent is None:
            agent_storage = self._storage.get_storage(Agent)
            agent = await agent_storage.get(binding.agent_id)
            if agent is None:
                raise NotFoundError(
                    f"Agent {binding.agent_id!r} not found for session "
                    f"{session.id!r}"
                )

        # Resolve the LLM adapter via the provider registry (cached).
        llm = await self._provider_registry.get_llm(agent.model.provider_id)

        # Resolve the LLMModel (provider's config row carries the
        # context_length); used by the compaction strategy. The agent's
        # ``model.model_name`` is the provider-side identifier.
        llm_model = await self._resolve_llm_model(agent)

        # Resolve every toolset the agent registered. Each id resolves
        # through the provider registry which also covers the reserved
        # ids (``_system``, ``_workspaces``, ``_search``).
        toolset_providers: dict = {}
        for toolset_id in (agent.tools or []):
            provider = await self._provider_registry.get_toolset(toolset_id)
            toolset_providers[toolset_id] = provider

        # Get the on-disk AgentSession the API allocated at create
        # time (Wave 2). The id matches session.id.
        agent_session = await workspace.get_session(session.id)
        if agent_session is None:
            raise NotFoundError(
                f"On-disk session slot for {session.id!r} missing on "
                f"workspace {workspace.id!r}; was it allocated via "
                "Workspace.start_session(..., id=sid)?"
            )

        # Build a workspace-aware ToolExecutionManager. The factory
        # composes the agent's toolsets with the session's workspace
        # tools and binds them to this AgentSession.
        tool_manager = ToolExecutionManager.for_workspace(
            toolset_providers=toolset_providers,
            session=agent_session,
        )

        executor = WorkspaceAgentExecutor(
            agent=agent,
            llm=llm,
            llm_model=llm_model,
            tool_manager=tool_manager,
            session=agent_session,
        )
        return _TurnDriver(executor)

    async def _build_graph_executor(self, session: Session, workspace):
        """Construct a graph executor for ``session``.

        DEFERRED — :class:`WorkspaceGraphExecutor` requires multi-node
        agent + LLM + tool-manager resolvers and a :class:`StateRepo`
        handle off the workspace. The agent path is the v1 priority
        (per the spec self-review notes); graph wiring lands in a
        dedicated follow-on sub-project so the surface here can stay
        focused on the dispatch decision.
        """
        raise NotImplementedError(
            "graph executor wiring is the next sub-project; v1 ships "
            "only the agent binding path. See workspace-graph executor "
            "constructor in matrix/graph/workspace_executor.py for the "
            "shape of the per-node resolver fan-out that needs wiring."
        )

    async def _resolve_llm_model(self, agent):
        """Look up the :class:`LLMModel` row matching ``agent.model``.

        Walks the configured :class:`LLMProvider`'s ``models`` list and
        returns the entry whose ``name`` matches ``agent.model.model_name``.
        Raises :class:`ConfigError` if the provider doesn't list the
        requested model name.
        """
        from matrix.model.except_ import ConfigError, NotFoundError
        from matrix.model.provider import LLMProvider

        provider_storage = self._storage.get_storage(LLMProvider)
        provider_row = await provider_storage.get(agent.model.provider_id)
        if provider_row is None:
            raise NotFoundError(
                f"LLMProvider {agent.model.provider_id!r} not found "
                f"for agent {agent.id!r}"
            )
        for m in provider_row.models:
            if m.name == agent.model.model_name:
                return m
        raise ConfigError(
            f"LLMProvider {agent.model.provider_id!r} does not list "
            f"model {agent.model.model_name!r}; configured models: "
            f"{[m.name for m in provider_row.models]}"
        )

    def _infer_post_turn_status(self, executor, session: Session) -> SessionStatus:
        """Map the executor's last ``Done.stop_reason`` to a SessionStatus.

        :class:`WorkspaceAgentExecutor` exposes the trailing stop reason
        via :attr:`last_done_reason` (set after each ``invoke`` call).
        The mapping mirrors what the executor itself decides for the
        cases it handles:

        * ``'end_turn'`` / ``'stop'`` / ``'stop_sequence'`` -> RUNNING
          (more user-driven turns may follow).
        * ``'tool_use'`` -> RUNNING (next turn dispatches tools).
        * ``'max_tokens'`` / ``'error'`` / ``'content_filter'`` ->
          WAITING (operator inspection needed).
        * ``None`` (e.g. fake test executors that never iterate) ->
          RUNNING (default; preserves the legacy behaviour).

        Workspace-side WAITING transitions for explicit waits
        (user-input prompt heuristic, tool-approval hand-off) are set
        INSIDE :meth:`WorkspaceAgentExecutor.invoke` via
        :meth:`AgentSession.set_status`. The post-turn re-read here
        only handles cases where the executor exited cleanly without
        having taken a wait.
        """
        last_reason = getattr(executor, "last_done_reason", None)
        if last_reason in ("max_tokens", "error", "content_filter"):
            return SessionStatus.WAITING
        return SessionStatus.RUNNING

    async def _run_one_turn(self, lease: Lease) -> None:
        """Execute one turn for the leased session.

        Failure paths (CancelledError, TransientError, fatal Exception) are
        handled in Task 17. This task ships only the happy path + the
        ENDED / cancel_requested / pause_requested early-exit checks.
        """
        sid = lease.session_id
        self._in_flight.add(sid)
        scope = _CancelScope()
        self._active_scopes[sid] = scope
        outcome: str = "success"
        started = time.monotonic()
        try:
            session = await self._load_session(sid)
            if session is None or session.status == SessionStatus.ENDED:
                await self._scheduler.complete_turn(
                    self._worker_id, sid,
                    expected_turn_no=lease.turn_no,
                    new_status=SessionStatus.ENDED,
                    re_enqueue=False,
                )
                outcome = "success"
                return

            # Cancel/pause requested between enqueue and claim — honour
            # without running a turn. Saves an LLM call on a session the
            # user already gave up on; also handles the race where the API
            # set the flag while we were spinning up.
            if session.cancel_requested:
                await self._scheduler.complete_turn(
                    self._worker_id, sid,
                    expected_turn_no=lease.turn_no,
                    new_status=SessionStatus.ENDED,
                    ended_reason="cancelled",
                    re_enqueue=False,
                )
                outcome = "cancelled"
                return
            if session.pause_requested:
                await self._scheduler.complete_turn(
                    self._worker_id, sid,
                    expected_turn_no=lease.turn_no,
                    new_status=SessionStatus.PAUSED,
                    re_enqueue=False,
                )
                outcome = "success"
                return

            workspace = await self._load_workspace_for_persist(session.workspace_id)
            executor = await self._build_executor(session, workspace)

            try:
                async with scope:
                    await executor.invoke([])
            except asyncio.CancelledError:
                await self._handle_cancel(lease, session)
                outcome = "cancelled"
                raise
            except TransientError as exc:
                await self._handle_transient(lease, session, exc)
                outcome = "failed"
                return
            except Exception as exc:  # noqa: BLE001
                await self._handle_fatal(lease, session, exc)
                outcome = "failed"
                return

            new_status = self._infer_post_turn_status(executor, session)
            result = await self._scheduler.complete_turn(
                self._worker_id, sid,
                expected_turn_no=lease.turn_no,
                new_status=new_status,
                re_enqueue=(new_status == SessionStatus.RUNNING),
            )
            if result is CompleteTurnResult.LEASE_LOST:
                outcome = "lease_lost"
            elif result is CompleteTurnResult.TURN_CONFLICT:
                outcome = "turn_conflict"
            else:
                outcome = "success"
            if result is not CompleteTurnResult.SUCCESS:
                logger.warning(
                    "turn-complete %s for session %s", result.value, sid,
                )
        finally:
            duration = time.monotonic() - started
            self._turn_duration_seconds_total += duration
            self._turn_duration_count += 1
            self._turns_total_by_result[outcome] = (
                self._turns_total_by_result.get(outcome, 0) + 1
            )
            self._active_scopes.pop(sid, None)
            self._in_flight.discard(sid)
            self._wake.set()

    async def _handle_transient(
        self, lease: Lease, session: Session, exc: BaseException,
    ) -> None:
        """Retryable failure path: classify, bump attempt_count, re-enqueue
        with exponential backoff. Past ``max_attempts``, end as failed."""
        new_attempt = (session.attempt_count or 0) + 1
        failure = FailureRecord(error_text=str(exc), attempt_count=new_attempt)
        if new_attempt >= self.config.max_attempts:
            await self._scheduler.complete_turn(
                self._worker_id, lease.session_id,
                expected_turn_no=lease.turn_no,
                new_status=SessionStatus.ENDED,
                ended_reason="failed",
                re_enqueue=False,
                record_failure=failure,
            )
            return
        backoff = compute_backoff(
            attempt=new_attempt,
            base=self.config.base_backoff_seconds,
            cap=self.config.max_backoff_seconds,
        )
        await self._scheduler.complete_turn(
            self._worker_id, lease.session_id,
            expected_turn_no=lease.turn_no,
            new_status=SessionStatus.RUNNING,
            re_enqueue=True,
            backoff=timedelta(seconds=backoff),
            record_failure=failure,
        )

    async def _handle_fatal(
        self, lease: Lease, session: Session, exc: BaseException,
    ) -> None:
        """Non-retryable failure path: end the session as failed."""
        logger.exception("session %s failed fatally", lease.session_id)
        failure = FailureRecord(
            error_text=f"{type(exc).__name__}: {exc}",
            attempt_count=(session.attempt_count or 0) + 1,
        )
        await self._scheduler.complete_turn(
            self._worker_id, lease.session_id,
            expected_turn_no=lease.turn_no,
            new_status=SessionStatus.ENDED,
            ended_reason="failed",
            re_enqueue=False,
            record_failure=failure,
        )

    async def _handle_cancel(
        self, lease: Lease, session: Session,
    ) -> None:
        """Mid-turn cancel arrived. session.cancel_requested -> ENDED;
        otherwise (pause_requested OR scope.cancel without flag) -> PAUSED."""
        if session.cancel_requested:
            new_status = SessionStatus.ENDED
            ended_reason = "cancelled"
        else:
            new_status = SessionStatus.PAUSED
            ended_reason = None
        await self._scheduler.complete_turn(
            self._worker_id, lease.session_id,
            expected_turn_no=lease.turn_no,
            new_status=new_status,
            ended_reason=ended_reason,
            re_enqueue=False,
        )


class _TurnDriver:
    """Adapter that consumes a streaming executor for the turn-based pool.

    The workspace executors expose ``invoke`` as an async generator
    (yielding :class:`StreamEvent`s), but the worker's
    :meth:`WorkerPool._run_one_turn` calls ``await executor.invoke([])``
    — a single-shot coroutine. This adapter bridges the two: drain the
    generator to completion inside an awaitable ``invoke`` and surface
    :attr:`last_done_reason` from the underlying executor for the
    post-turn status mapper to read.
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
