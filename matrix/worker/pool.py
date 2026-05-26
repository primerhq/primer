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
from matrix.model.yield_ import YieldToWorker
from matrix.worker.turn import _CancelScope, compute_backoff
from matrix.worker.yield_resume_registry import get_resume_hook
from matrix.worker.yield_runtime import (
    _dispatch_to_channels,
    _resume_tool_approval,
    classify_resume_payload,
    ParkedState,
)

if TYPE_CHECKING:
    from matrix.agent.approval import ApprovalResolver
    from matrix.api.registries import ProviderRegistry, WorkspaceRegistry
    from matrix.graph.router import RouterRegistry
    from matrix.int.storage_provider import StorageProvider

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
        prefix = sid.split("__", 1)[0]
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
        router_registry: "RouterRegistry | None" = None,
        approval_resolver: "ApprovalResolver | None" = None,
        channel_dispatcher=None,
    ) -> None:
        self.config = config
        self._scheduler = scheduler
        self._storage = storage
        self._workspace_registry = workspace_registry
        self._provider_registry = provider_registry
        # Optional RouterRegistry for callable-router edges in graph
        # dispatch. None means only _StaticEdge + _JsonPathRouter edges
        # work; _CallableRouter edges will raise at runtime.
        self._router_registry = router_registry
        self._approval_resolver = approval_resolver
        self._channel_dispatcher = channel_dispatcher

        self._worker_id: str = ""
        self._tasks: list[asyncio.Task] = []
        self._active_scopes: dict[str, _CancelScope] = {}
        self._in_flight: set[str] = set()
        # Strong references to in-flight per-turn tasks so the GC does not
        # silently collect them between create_task and the first await.
        self._turn_tasks: set[asyncio.Task] = set()
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
                # Reserve in_flight slots for the about-to-dispatch leases
                # BEFORE the next claim_loop iteration runs. Without this,
                # asyncio.create_task() returns immediately but the task
                # body's `_in_flight.add(sid)` happens later, so a fast
                # next iteration sees stale free-capacity and over-claims.
                # The duplicate `_in_flight.add` inside _run_one_turn is
                # idempotent on a set, so this is safe.
                self._in_flight.update(lease.session_id for lease in leases)
                for lease in leases:
                    # Fire-and-forget: _run_one_turn manages its own
                    # in_flight bookkeeping via the finally block. The
                    # task is retained in self._turn_tasks so the GC
                    # cannot silently drop it before it gets scheduled.
                    task = asyncio.create_task(
                        self._run_one_turn(lease),
                        name=f"turn-{lease.session_id}",
                    )
                    self._turn_tasks.add(task)
                    task.add_done_callback(self._turn_tasks.discard)
        except asyncio.CancelledError:
            return

    async def _notify_loop(self) -> None:
        """Drain ready notifications from the scheduler. Each one wakes the
        claim loop so it can attempt a fresh claim sooner than the poll.

        Wraps the inner ``async for`` in a restart loop: if the scheduler's
        watch generator raises an unexpected exception, log it and back off
        rather than killing the loop for the lifetime of the process.
        """
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                async for _sid in self._scheduler.watch_ready(self._worker_id):
                    self._wake.set()
                    if self._stopping.is_set():
                        return
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "notify_loop watch_ready raised; restarting in %.1fs",
                    backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, 30.0)
            else:
                # Generator exited cleanly (scheduler shutdown). Restart
                # only if we ourselves are not stopping.
                backoff = 1.0

    async def _cancel_loop(self) -> None:
        """Drain cancel notifications. When a sid arrives that this worker
        holds an active scope for, fire scope.cancel(reason). The reason
        string is informational; the worker re-loads the Session row inside
        _handle_cancel to determine cancel-vs-pause routing.

        Restart-on-failure pattern mirrors :meth:`_notify_loop`.
        """
        backoff = 1.0
        while not self._stopping.is_set():
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
                backoff = 1.0

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

        # agent.tools holds scoped tool ids (toolset_id__tool_name).
        # Derive the unique toolset prefixes so we only resolve the
        # toolset providers the agent actually needs.
        toolset_ids = _toolset_ids_from_scoped(agent.tools)
        toolset_providers: dict = {}
        for toolset_id in toolset_ids:
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
        # composes the agent's tool surface with the session's
        # workspace tools and binds them to this AgentSession. The
        # ``tools`` list is the agent's scoped-tool surface — the
        # manager exposes exactly those tools to the LLM and rejects
        # dispatch on anything else.
        tool_manager = ToolExecutionManager.for_workspace(
            toolset_providers=toolset_providers,
            session=agent_session,
            approval_resolver=self._approval_resolver,
            provider_registry=self._provider_registry,
            tools=agent.tools,
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
        """Build a turn-driver around :class:`WorkspaceGraphExecutor`.

        Resolves the graph (snapshot first, falls back to storage),
        the per-node agent + LLM + toolset resolvers (which mirror the
        agent path), the workspace's git-backed state repo (required —
        only :class:`matrix.workspace.local.LocalWorkspace` exposes
        one today; sandbox/container/k8s backends will need StateRepo
        parity before they can host graph dispatch), and an optional
        :class:`RouterRegistry` stashed on app.state at startup.

        Unlike the agent path, the graph executor runs the WHOLE
        graph in one ``invoke()`` call. The returned :class:`_GraphTurnDriver`
        always reports ``last_done_reason = "graph_ended"`` so the
        post-turn status mapper transitions the session straight to
        ``ENDED`` — no re-enqueue.

        Phase 2 scope:
            - graph_resolver wired — subgraph nodes resolve from storage
            - router_registry wired from app.state (None if no
              callable routers registered → callable-router edges raise)
            - workspace_session wired from the graph-holder slot
              allocated by POST /workspaces/{id}/sessions; agents in
              the graph receive composite system prompt augmentation
              + workspace tools per-node. Falls back to None for
              legacy graph-bound sessions created before the holder
              allocation landed.
        """
        from matrix.agent.tool_manager import ToolExecutionManager
        from matrix.graph.workspace_executor import WorkspaceGraphExecutor
        from matrix.model.agent import Agent
        from matrix.model.except_ import ConfigError, NotFoundError
        from matrix.model.graph import Graph

        binding = session.binding  # GraphSessionBinding

        # ① Resolve the Graph: snapshot first, then storage. Falls back
        # gracefully so the executor sees a consistent definition even
        # if the row is edited mid-session.
        graph = binding.graph_snapshot
        if graph is None:
            graph_storage = self._storage.get_storage(Graph)
            graph = await graph_storage.get(binding.graph_id)
            if graph is None:
                raise NotFoundError(
                    f"Graph {binding.graph_id!r} not found for session "
                    f"{session.id!r}"
                )

        # ② Workspace state-repo: required for the executor's git-backed
        # state persistence. Only LocalWorkspace exposes one today.
        # getattr-with-default tolerates legacy fakes that predate the
        # state_repo addition to the ABC.
        state_repo = getattr(workspace, "state_repo", None)
        if state_repo is None:
            raise ConfigError(
                f"workspace {workspace.id!r} ({type(workspace).__name__}) "
                "does not expose a state_repo; graph-bound sessions "
                "currently require a LocalWorkspace. Sandbox / Container "
                "/ K8s backends need StateRepo parity (a tracked follow-on)."
            )

        # ③ Per-node resolvers — closures over self so each resolver
        # can use the same provider/storage caches as the agent path.

        async def agent_resolver(agent_id: str):
            agent_storage = self._storage.get_storage(Agent)
            row = await agent_storage.get(agent_id)
            if row is None:
                raise NotFoundError(
                    f"Agent {agent_id!r} referenced by graph "
                    f"{graph.id!r} not found"
                )
            return row

        async def llm_resolver(agent):
            llm = await self._provider_registry.get_llm(
                agent.model.provider_id
            )
            llm_model = await self._resolve_llm_model(agent)
            return llm, llm_model

        # ④ Holder AgentSession allocated by POST /workspaces/{id}/sessions
        # (Phase 2). Optional — fall back to None for legacy graph-
        # bound sessions that were created before holder allocation
        # landed. With the holder, agents in the graph get composite
        # system prompt augmentation + workspace tools per-node.
        workspace_session = await workspace.get_session(session.id)

        async def tool_manager_resolver(agent):
            toolset_ids = _toolset_ids_from_scoped(agent.tools)
            toolset_providers: dict = {}
            for toolset_id in toolset_ids:
                provider = await self._provider_registry.get_toolset(
                    toolset_id
                )
                toolset_providers[toolset_id] = provider
            if workspace_session is not None:
                return ToolExecutionManager.for_workspace(
                    toolset_providers=toolset_providers,
                    session=workspace_session,
                    approval_resolver=self._approval_resolver,
                    provider_registry=self._provider_registry,
                    tools=agent.tools,
                )
            return ToolExecutionManager(
                toolset_providers=toolset_providers,
                approval_resolver=self._approval_resolver,
                provider_registry=self._provider_registry,
                tools=agent.tools,
            )

        # ④ Optional handles wired in later phases.

        async def graph_resolver(subgraph_id: str):
            graph_storage = self._storage.get_storage(Graph)
            row = await graph_storage.get(subgraph_id)
            if row is None:
                raise NotFoundError(
                    f"Subgraph {subgraph_id!r} referenced by graph "
                    f"{graph.id!r} not found"
                )
            return row

        # RouterRegistry singleton stashed on app.state at startup
        # (None if no callables registered). Pass through; the
        # executor only needs it for _CallableRouter edges.
        router_registry = getattr(self, "_router_registry", None)

        executor = WorkspaceGraphExecutor(
            graph=graph,
            agent_resolver=agent_resolver,
            llm_resolver=llm_resolver,
            tool_manager_resolver=tool_manager_resolver,
            state_repo=state_repo,
            graph_session_id=session.id,
            workspace_session=workspace_session,
            graph_resolver=graph_resolver,
            router_registry=router_registry,
            principal=None,
        )
        return _GraphTurnDriver(executor)

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
        # Graph dispatch sets a sentinel — the graph executor runs the
        # whole graph in one invoke() call, so there's no follow-up
        # turn for the worker to schedule.
        if last_reason == "graph_ended":
            return SessionStatus.ENDED
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
                # Cancel-during-park (spec §7.3 step 3 / §7.4): if the
                # row is parked or resumable, NULL the parked columns
                # before ending so a future inspector doesn't see a
                # dead park blob on an ENDED row.
                if session.parked_status is not None:
                    await self._scheduler.clear_park(sid)
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

            # Yielding-tools resume branch (spec §7.3). If the scheduler
            # handed us a resumable row, the park's event has fired —
            # rehydrate parked_state, dispatch the resume hook, persist
            # the synthesised tool_result into history, clear park
            # columns, re-enqueue. The continuation LLM call runs on
            # the NEXT normal claim against the augmented history.
            if session.parked_status == "resumable":
                await self._handle_resume(lease, session)
                outcome = "resumed"
                return

            workspace = await self._load_workspace_for_persist(session.workspace_id)

            try:
                executor = await self._build_executor(session, workspace)
                async with scope:
                    await executor.invoke([])
            except asyncio.CancelledError:
                await self._handle_cancel(lease, session)
                outcome = "cancelled"
                raise
            except YieldToWorker as yield_exc:
                # The agent invoked a yielding tool; the LLM loop
                # raised after stamping the Yielded sentinel. Park
                # the session in storage and release our lease — a
                # later worker resumes when the event fires.
                await self._handle_yield(lease, session, yield_exc)
                outcome = "parked"
                return
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
            if result is CompleteTurnResult.TURN_CONFLICT:
                # Two workers ran the same session turn -- the scheduler's
                # claim/lease invariant has been violated. Promote to ERROR
                # so this does not get lost in operational noise.
                logger.error(
                    "turn-complete TURN_CONFLICT for session %s "
                    "(expected_turn_no=%d) -- claim/lease invariant violated",
                    sid, lease.turn_no,
                )
            elif result is CompleteTurnResult.LEASE_LOST:
                logger.warning(
                    "turn-complete LEASE_LOST for session %s "
                    "(another worker stole the lease)", sid,
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

    async def _handle_yield(
        self,
        lease: Lease,
        session: Session,
        yield_exc: YieldToWorker,
    ) -> None:
        """Park the in-flight turn (yielding-tools §7.2).

        The tool engine raised :class:`YieldToWorker` because the
        agent invoked a yielding tool. Package the in-progress turn
        state into the parked_state blob and call the scheduler's
        ``park_turn`` to atomically write the park + release the
        lease.

        The blob carries enough state for any worker (same or
        different) to resume the turn once the event fires:

        * ``yielded`` — the Yielded sentinel (tool name, event key,
          timeout, resume_metadata)
        * ``llm_messages`` — the in-progress LLM history; for M1
          we leave this as an empty list because the executor
          doesn't yet expose it. M2+ will plumb the real history
          through.
        * ``turn_no`` / ``started_at`` — for audit + elapsed
          calculations.

        The worker also stamps ``parked_at_iso`` into the tool's
        ``resume_metadata`` so the resume hook can compute elapsed
        time without re-reading the session row.
        """
        from datetime import datetime, timedelta, timezone

        from matrix.worker.yield_runtime import ParkedState

        yielded = yield_exc.yielded
        parked_at = datetime.now(timezone.utc)
        # Per-yield timeout takes precedence; fall back to the global
        # yield cap (60 min default — overridable via env once
        # config is wired in M2).
        timeout = (
            yielded.timeout
            if yielded.timeout is not None
            else 3600.0
        )
        parked_until = parked_at + timedelta(seconds=timeout)

        # Inject parked_at_iso into resume_metadata so the resume
        # hook can compute elapsed without a separate read.
        resume_metadata = dict(yielded.resume_metadata)
        resume_metadata["parked_at_iso"] = parked_at.isoformat()
        yielded_stamped = type(yielded)(
            tool_name=yielded.tool_name,
            event_key=yielded.event_key,
            timeout=yielded.timeout,
            resume_metadata=resume_metadata,
        )

        # In-progress turn messages: the executor stamps
        # ``YieldToWorker.llm_messages`` with the assistant message
        # that emitted the tool_use (loop.py appends it before
        # _dispatch_tool_calls). Round-trip through model_dump so
        # the JSONB column carries canonical Matrix message-dicts
        # — ParkedState.from_jsonable rebuilds typed Messages on
        # resume. Tools that synthesise their result from metadata
        # alone (e.g. sleep) work even with an empty list; tools
        # that need the tool_use in history (e.g. _approval's
        # synthesised tool_result message) rely on this being
        # populated.
        captured_messages = yield_exc.llm_messages or []
        llm_message_dicts = [
            m.model_dump(mode="json") for m in captured_messages
        ]

        parked_state = ParkedState(
            yielded=yielded_stamped,
            llm_messages=llm_message_dicts,
            turn_no=lease.turn_no,
            started_at=parked_at,
            tool_call_id=yield_exc.tool_call_id,
        )

        logger.info(
            "session %s parking on tool %r (event_key=%r, timeout=%.1fs)",
            lease.session_id,
            yielded.tool_name,
            yielded.event_key,
            timeout,
        )

        await self._scheduler.park_turn(
            self._worker_id,
            lease.session_id,
            expected_turn_no=lease.turn_no,
            parked_event_key=yielded.event_key,
            parked_until=parked_until,
            parked_at=parked_at,
            parked_state=parked_state.to_jsonable(),
        )

        if self._channel_dispatcher is not None:
            asyncio.create_task(
                _dispatch_to_channels(
                    dispatcher=self._channel_dispatcher,
                    session=session,
                    yielded=yielded,
                )
            )

    async def _handle_resume(
        self, lease: Lease, session: Session,
    ) -> None:
        """Drive a resumable park to its conclusion (spec §7.3).

        The scheduler claim path admits ``parked_status='resumable'``
        rows; this branch is the dispatch:

          1. Rehydrate ``ParkedState`` from the blob.
          2. Classify the resume payload (real event / timeout /
             cancelled).
          3. Call the resume hook — ``_resume_tool_approval`` inline
             for the special ``_approval`` tool name, the registry's
             ``get_resume_hook`` for everything else.
          4. Persist [rehydrated_assistant_with_tool_use,
             synthesised_tool_result] via the executor's
             ``inject_resume_messages``.
          5. ``clear_park`` to NULL parked columns.
          6. ``complete_turn(RUNNING, re_enqueue=True)`` so the next
             normal claim drives the continuation LLM turn.

        Imports happen lazily inside the function body so this module
        doesn't pull executor / chat-model deps at startup. Mirrors
        the pattern used by ``_build_executor`` for consistency.

        Graph-bound sessions don't park in production (spec §10), but
        defensively: if one arrives here we end the row as ``failed``
        rather than reach into a path the graph executor doesn't
        expose.
        """
        # Imports are lazy: yield_runtime imports chat models, which
        # pulls in pydantic etc. Worker pool startup avoids that.
        import json
        from matrix.model.chat import Message, ToolResultPart

        sid = session.id

        # Defensive guard: graph-bound sessions don't have an
        # `inject_resume_messages` surface (graph executor runs to
        # completion in one turn). Treat this as a programming bug
        # rather than silently mis-resuming.
        if session.binding.kind != "agent":
            logger.error(
                "resume: non-agent session %s arrived at the resume "
                "branch with parked_state — clearing park and ending "
                "as failed (graph sessions are not supposed to park)",
                sid,
            )
            await self._scheduler.clear_park(sid)
            await self._scheduler.complete_turn(
                self._worker_id, sid,
                expected_turn_no=lease.turn_no,
                new_status=SessionStatus.ENDED,
                ended_reason="failed",
                re_enqueue=False,
            )
            return

        # ----- Rehydrate ParkedState from the JSONB blob ------------
        blob = session.parked_state or {}
        try:
            parked = ParkedState.from_jsonable(blob)
        except (KeyError, ValueError, TypeError):
            logger.exception(
                "resume: malformed parked_state for session %s — "
                "clearing park + failing the session",
                sid,
            )
            await self._scheduler.clear_park(sid)
            await self._scheduler.complete_turn(
                self._worker_id, sid,
                expected_turn_no=lease.turn_no,
                new_status=SessionStatus.ENDED,
                ended_reason="failed",
                re_enqueue=False,
            )
            return

        if session.parked_at is None:
            # Shouldn't happen — parked_status==resumable but no
            # parked_at means the park-write was malformed. Fail
            # closed for the same reason as the rehydrate branch.
            logger.error(
                "resume: session %s has parked_status=resumable but "
                "parked_at=None — failing the session",
                sid,
            )
            await self._scheduler.clear_park(sid)
            await self._scheduler.complete_turn(
                self._worker_id, sid,
                expected_turn_no=lease.turn_no,
                new_status=SessionStatus.ENDED,
                ended_reason="failed",
                re_enqueue=False,
            )
            return

        resume_payload = classify_resume_payload(
            parked, parked_at=session.parked_at,
        )

        # ----- Build the agent executor + tool_manager --------------
        workspace = await self._load_workspace_for_persist(
            session.workspace_id,
        )
        # _build_agent_executor returns a _TurnDriver wrapping a
        # WorkspaceAgentExecutor. The resume path needs the inner
        # executor to call inject_resume_messages + (for _approval)
        # the inner _tool_manager to re-dispatch with bypass_approval.
        executor_or_driver = await self._build_agent_executor(
            session, workspace,
        )
        # Driver wraps executor as `_executor`; the test-fakes pass
        # the executor through directly. Tolerate both.
        executor = getattr(executor_or_driver, "_executor", executor_or_driver)
        tool_manager = getattr(executor, "_tool_manager", None)

        tool_name = parked.yielded.tool_name

        # ----- Dispatch the resume hook -----------------------------
        # Resume-hook failures are caught and synthesised as
        # ToolResultPart(error=True) so the agent's LLM sees the
        # failure in history instead of the turn crashing fatally.
        # Two classes of failure this catches:
        #   * Approved-path bypass dispatch on a tool that no longer
        #     exists (operator wrote a policy for a tool that's
        #     since been removed, or never existed — there's no FK
        #     from ToolApprovalPolicy.tool_name to a registered
        #     tool). Without this catch, UnsupportedContentError
        #     escapes _run_one_turn into the fatal handler.
        #   * Resume-hook itself raises (malformed metadata, etc.).
        try:
            if tool_name == "_approval":
                # The approval gate's resume needs a live
                # ToolExecutionManager to re-dispatch the original
                # call with bypass_approval=True on the approved
                # path. Falls through to synthetic rejection on
                # YieldTimeout / YieldCancelled / malformed.
                tool_result_part = await _resume_tool_approval(
                    blob=blob,
                    payload=resume_payload.payload,
                    tool_manager=tool_manager,
                )
            else:
                # Generic resume hook from the registry. Hook may be
                # sync (current convention for sleep/ask_user/
                # watch_files) or async — await if awaitable.
                hook = get_resume_hook(tool_name)
                hook_result = hook(
                    parked.yielded.resume_metadata,
                    resume_payload.payload,
                )
                if asyncio.iscoroutine(hook_result):
                    hook_result = await hook_result
                # ToolCallResult -> ToolResultPart (line-for-line
                # per matrix/model/chat.py:256's documented recipe).
                tool_result_part = ToolResultPart(
                    id=parked.tool_call_id or "unknown",
                    output=hook_result.output,
                    error=hook_result.is_error,
                )
        except Exception as exc:  # noqa: BLE001 — fail-closed synthesis
            logger.exception(
                "resume: hook for tool %r on session %s raised; "
                "synthesising error tool_result so the agent sees "
                "the failure in history",
                tool_name, sid,
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

        # ----- Persist the [assistant_tool_use, tool_result] pair ----
        # The assistant message that emitted the tool_use lives in
        # parked_state.llm_messages (stamped by the executor at park
        # time — see matrix/agent/base.py). Rehydrate as typed
        # Messages, then build the matching tool-role message.
        rehydrated_assistant = [
            Message.model_validate(m) for m in parked.llm_messages
        ]
        tool_result_msg = Message(
            role="tool",
            parts=[tool_result_part],
        )
        # Persist + clean transition. If commit_state rejects (e.g.
        # the on-disk AgentSession is already ENDED from a prior
        # failed turn — possible after a fatal LLM error in the
        # preceding cycle), fall through to a clean terminal state:
        # clear_park so the row isn't stuck as a `resumable` orphan,
        # and end the session as failed. Without this branch the
        # row sits forever at parked_status='resumable' with the
        # bus event re-armed but no consumer that can ever succeed.
        try:
            await executor.inject_resume_messages(
                [*rehydrated_assistant, tool_result_msg],
            )
        except Exception:
            logger.exception(
                "resume: persist failed for session %s — clearing "
                "park and ending session as failed so the row isn't "
                "stuck at parked_status='resumable'",
                sid,
            )
            await self._scheduler.clear_park(sid)
            await self._scheduler.complete_turn(
                self._worker_id, sid,
                expected_turn_no=lease.turn_no,
                new_status=SessionStatus.ENDED,
                ended_reason="failed",
                re_enqueue=False,
            )
            return

        # ----- Clear park + re-enqueue ------------------------------
        await self._scheduler.clear_park(sid)
        await self._scheduler.complete_turn(
            self._worker_id, sid,
            expected_turn_no=lease.turn_no,
            new_status=SessionStatus.RUNNING,
            re_enqueue=True,
        )

    async def _handle_cancel(
        self, lease: Lease, session: Session,
    ) -> None:
        """Mid-turn cancel arrived. ``session.cancel_requested`` -> ENDED;
        otherwise (``pause_requested`` OR scope.cancel without a flag)
        -> PAUSED.

        The ``session`` argument is a snapshot from the start of the turn;
        the user may have set ``cancel_requested`` after the turn began,
        so we re-load the row and prefer the fresh state. The
        ``complete_turn`` write is shielded from a second cancel so a
        drain-timeout cannot leave the lease pinned to this worker.
        """
        fresh = await self._load_session(lease.session_id)
        if fresh is not None:
            session = fresh
        if session.cancel_requested:
            new_status = SessionStatus.ENDED
            ended_reason = "cancelled"
        else:
            new_status = SessionStatus.PAUSED
            ended_reason = None
        await asyncio.shield(
            self._scheduler.complete_turn(
                self._worker_id, lease.session_id,
                expected_turn_no=lease.turn_no,
                new_status=new_status,
                ended_reason=ended_reason,
                re_enqueue=False,
            )
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


class _GraphTurnDriver:
    """Adapter for :class:`matrix.graph.workspace_executor.WorkspaceGraphExecutor`.

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
