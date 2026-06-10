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
from primer.worker.yield_resume_registry import get_resume_hook
from primer.worker.yield_runtime import (
    _resume_tool_approval,
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
                    except asyncio.TimeoutError:
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
                    except asyncio.TimeoutError:
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
                else:
                    outcome = await self._resume_engine_session(engine_lease, session_row)
            else:
                outcome = await run_one_session_turn(engine_lease, deps)
        except Exception:
            logger.exception(
                "run_one_session_turn for session %s raised unexpectedly",
                sid,
            )
        finally:
            self._in_flight.discard((ClaimKind.SESSION, sid))
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
        from primer.int.claim import ReleaseOutcome
        from primer.model.chat import Message, ToolResultPart

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

        tool_name = parked.yielded.tool_name
        try:
            if tool_name == "_approval":
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

        rehydrated_assistant = [Message.model_validate(m) for m in parked.llm_messages]
        tool_result_msg = Message(role="tool", parts=[tool_result_part])
        try:
            await executor.inject_resume_messages(
                [*rehydrated_assistant, tool_result_msg],
            )
        except Exception:
            logger.exception(
                "resume: persist failed for session %s - ending failed", sid,
            )
            return await self._end_session(session, reason="failed")

        # Continuation: clear park (on_release) + keep the lease so the next
        # claim runs the continuation LLM turn.
        return ReleaseOutcome(success=True, drop_lease=False)

    async def _resume_graph_engine(self, session, parked):
        """Resume a graph-bound session parked at a ToolCall approval.

        Adapted from the (dead) _handle_graph_resume: always terminal (graph
        sessions run to completion in one resume), so this returns a drop-lease
        outcome with ENDED status written to the row."""
        from primer.worker.graph_resume import resume_graph_from_checkpoint

        sid = session.id
        assert parked.graph_checkpoint is not None

        if session.parked_at is None:
            logger.error(
                "resume: graph session %s resumable but parked_at=None -"
                " ending failed", sid,
            )
            return await self._end_session(session, reason="failed")

        resume_payload = classify_resume_payload(parked, parked_at=session.parked_at)
        workspace = await self._load_workspace_for_persist(session.workspace_id)
        try:
            executor_or_driver = await self._build_graph_executor(session, workspace)
        except Exception:
            logger.exception(
                "resume: failed to build graph executor for session %s -"
                " ending failed", sid,
            )
            return await self._end_session(session, reason="failed")
        executor = getattr(executor_or_driver, "_executor", executor_or_driver)

        reason = "completed"
        try:
            decision = await resume_graph_from_checkpoint(
                executor=executor,
                checkpoint=parked.graph_checkpoint,
                payload=resume_payload.payload,
            )
            if decision != "approved":
                reason = "failed"
        except Exception:
            logger.exception(
                "resume: graph executor for session %s raised during resume"
                " drain - ending failed", sid,
            )
            reason = "failed"

        return await self._end_session(session, reason=reason)

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

        deps = ChatDispatchDeps(
            storage_provider=self._storage,
            provider_registry=self._provider_registry,
            event_bus=self._event_bus,
            chat_tick_router=self._chat_tick_router,
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

    async def _build_session_executor(self, session: WorkspaceSession):
        """Callable passed as ``SessionDispatchDeps.build_executor``.

        Resolves the workspace for ``session.workspace_id`` then delegates
        to :meth:`_build_executor`. The dispatch path consumes the
        executor's streaming ``invoke()`` via ``async for``, so we
        unwrap the legacy ``_TurnDriver``/``_GraphTurnDriver`` shim
        (which exposes ``invoke`` as a non-iterable coroutine for the
        old ``_run_one_turn`` path) and return the underlying streaming
        executor.
        """
        workspace = await self._load_workspace_for_persist(session.workspace_id)
        wrapped = await self._build_executor(session, workspace)
        inner = getattr(wrapped, "_executor", None)
        return inner if inner is not None else wrapped

    async def _build_agent_executor(self, session: WorkspaceSession, workspace):
        """Build a turn-driver around :class:`WorkspaceAgentExecutor`.

        Resolves the agent definition (snapshot first, falls back to
        storage), the LLM via the provider registry, every toolset the
        agent registered, and the on-disk :class:`AgentSession` slot
        the API allocated at create time (id = ``session.id``).

        Returns a small adapter (not the executor itself) that exposes
        an awaitable ``invoke(messages)`` and a ``last_done_reason``
        attribute. The adapter consumes the executor's async-generator
        ``invoke`` to completion so the worker can await it as a single
        coroutine rather than iterating the stream directly.
        """
        from primer.agent.tool_manager import ToolExecutionManager
        from primer.agent.workspace_executor import WorkspaceAgentExecutor
        from primer.model.agent import Agent
        from primer.model.except_ import NotFoundError

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

    async def _build_graph_executor(self, session: WorkspaceSession, workspace):
        """Build a turn-driver around :class:`WorkspaceGraphExecutor`.

        Resolves the graph (snapshot first, falls back to storage),
        the per-node agent + LLM + toolset resolvers (which mirror the
        agent path), the workspace's git-backed state repo (required —
        only :class:`primer.workspace.local.LocalWorkspace` exposes
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
        from primer.agent.tool_manager import ToolExecutionManager
        from primer.graph.workspace_executor import WorkspaceGraphExecutor
        from primer.model.agent import Agent
        from primer.model.except_ import ConfigError, NotFoundError
        from primer.model.graph import Graph

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
                "require a workspace with StateRepo support "
                "(LocalWorkspace or SandboxWorkspace)."
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
        from primer.model.except_ import ConfigError, NotFoundError
        from primer.model.provider import LLMProvider

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

    def _infer_post_turn_status(self, executor, session: WorkspaceSession) -> SessionStatus:
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
