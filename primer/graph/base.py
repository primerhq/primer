"""Shared base class for graph executors.

The :class:`_BaseGraphExecutor` is intentionally non-public (leading
underscore in the name); concrete executors are
:class:`primer.graph.GraphExecutor` (storage-backed) and
:class:`primer.graph.WorkspaceGraphExecutor` (workspace-backed).

Behaviour:

* Pregel-style superstep loop -- compute the ready set, run all
  ready nodes concurrently, evaluate outgoing edges, recompute the
  ready set, increment the iteration counter.
* Per-node LLM dispatch goes through :func:`primer.agent.loop.run_agent_turn`
  so tool dispatch inside a graph node is identical to a standalone
  agent's tool dispatch (multi-turn loops, error mapping, etc).
* Live streaming -- every event from every concurrently-running node
  is wrapped in :class:`ExtendedEvent(_GraphNodeEvent(...))` and
  pushed to a per-superstep :class:`asyncio.Queue` so the caller's
  iterator yields events as they happen rather than at the end of
  the superstep.
* Subgraph execution -- :class:`_GraphNodeRef` nodes recurse via the
  subclass's :meth:`_build_sub_executor` hook. Sub-events are
  re-wrapped with the parent node's id so taps still see "subgraph
  X is running".
* Edge evaluation -- static edges always fire; conditional edges
  consult the registered router (JSON-path or callable).
* Cycle bound -- aborts with ``ended_reason="max_iterations_exceeded"``
  when ``graph.max_iterations`` is hit.
* State persistence happens at superstep boundaries through the
  subclass's hooks; concrete executors decide whether each save is
  a plain file write or a git-versioned commit.

Subclasses provide four abstract hooks:

* :meth:`_load_node_history`
* :meth:`_persist_node_turn`
* :meth:`_save_state`
* :meth:`_build_sub_executor`
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.agent.loop import run_agent_turn
from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.router import RouterRegistry, first_matching_branch
from primer.graph.template import render_input_template
from primer.model.chat import (
    ExtendedEvent,
    Message,
    StreamEvent,
    TextPart,
    ToolResultPart,
    _GraphNodeEvent,
)
from primer.model.except_ import ConfigError
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphContext,
    NodeOutput,
    NodeRuntimeState,
    NodeRuntimeStatus,
    _AgentNodeRef,
    _BeginNode,
    _CallableRouter,
    _ConditionalEdge,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _GraphNodeRef,
    _JsonPathRouter,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.problem_details import ProblemDetails
from primer.model.turn_log import (
    TurnLogCompleted,
    TurnLogFailed,
    TurnLogStarted,
    TurnLogSuperstepEnded,
    TurnLogSuperstepStarted,
)
from primer.model.workspace_session import SessionStatus
from primer.model.yield_ import YieldToWorker
from primer.observability.turn_log_writer import (
    NoopTurnLogWriter,
    TurnLogWriter,
    safe_append as _safe_graph_turn_log,
    to_problem_details,
)


if TYPE_CHECKING:
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.chat import ToolResultPart
    from primer.model.provider import LLMModel


logger = logging.getLogger(__name__)


# Module-level value types and pure helpers live in _node_refs; they are
# re-exported here so ``from primer.graph.base import _PendingToolCall`` (and
# the many tests / callers that import these names) keep working unchanged.
from primer.graph._node_refs import (  # noqa: E402
    _EndOutputResult,
    _FanInOutputResult,
    _FanoutDrainState,
    _FanoutInstance,
    _FanoutSourceInvalid,
    _GraphEndOutputEvent,
    _GraphErrorEvent,
    _GraphToolCallYield,
    _NodeDone,
    _PendingAgentYield,
    _PendingToolCall,
    _RoutingFailed,
    _ToolApprovalRejected,
    _ToolCallOutputResult,
    _map_toolcall_result,
    _materialise_begin_output,
    _render_end_output,
    _render_fanin_output,
    _resolve_fanout_spec,
    _resolve_initial_ready_node,
    _resolve_toolcall_arguments,
)
from primer.graph._checkpoint import _CheckpointMixin  # noqa: E402
from primer.graph._agent_node import _AgentNodeMixin  # noqa: E402


class _BaseGraphExecutor(_CheckpointMixin, _AgentNodeMixin, ABC):
    """Pregel-style graph runtime base class with live event streaming."""

    def __init__(
        self,
        *,
        graph: Graph,
        agent_resolver: Callable[[str], Awaitable["Agent"]],
        llm_resolver: Callable[["Agent"], Awaitable[tuple["LLM", "LLMModel"]]],
        tool_manager_resolver: Callable[
            ["Agent"], Awaitable[ToolExecutionManager]
        ] | None = None,
        graph_resolver: Callable[[str], Awaitable[Graph]] | None = None,
        router_registry: RouterRegistry | None = None,
        principal: str | None = None,
    ) -> None:
        self._graph = graph
        self._agent_resolver = agent_resolver
        self._llm_resolver = llm_resolver
        self._tool_manager_resolver = tool_manager_resolver
        self._graph_resolver = graph_resolver
        self._router_registry = router_registry or RouterRegistry()
        self._principal = principal
        # Lookup helpers built once at construction.
        self._nodes_by_id = {n.id: n for n in graph.nodes}
        self._edges_by_from: dict[str, list] = {}
        self._edges_by_to: dict[str, list] = {}
        # Source node ids that own a callable-router out-edge. A callable
        # router's target is unknown statically (it can return any node id at
        # run time), so it is NOT recorded in ``_edges_by_to``. Instead the
        # FanIn ready-set treats any such source as a *potential* upstream
        # while it is still live (admitted-but-not-yet-resolved): see
        # ``_fanin_ready``. Without this a FanIn fed by a callable router
        # could fire before that branch completed.
        self._callable_router_sources: set[str] = set()
        for e in graph.edges:
            self._edges_by_from.setdefault(e.from_node, []).append(e)
            # Only static + json-path conditional edges have statically-known
            # ``to_node``s; callable-router targets are tracked separately
            # (see ``_callable_router_sources`` above).
            if isinstance(e, _StaticEdge):
                self._edges_by_to.setdefault(e.to_node, []).append(e)
            elif isinstance(e, _ConditionalEdge):
                if isinstance(e.router, _JsonPathRouter):
                    for branch in e.router.branches:
                        self._edges_by_to.setdefault(branch.to_node, []).append(e)
                    if e.router.default_to is not None:
                        self._edges_by_to.setdefault(
                            e.router.default_to, []
                        ).append(e)
                elif isinstance(e.router, _CallableRouter):
                    self._callable_router_sources.add(e.from_node)
        # FanOut bookkeeping (Spec B §2.1):
        # ``_pending_fanout`` -- staged instances awaiting drain after the
        #   FanOut's own completion within the current superstep.
        # ``_fanout_instances`` -- map of synthesized_id -> _FanoutInstance for
        #   the executor's per-instance dispatch path.
        # ``_fanout_target_expected_count`` -- per fan-out target id, the
        #   expected number of synthesized instances (for FanIn ready-set).
        self._pending_fanout: dict[str, list[_FanoutInstance]] = {}
        self._fanout_instances: dict[str, _FanoutInstance] = {}
        self._fanout_target_expected_count: dict[str, int] = {}
        # FanOutSpec on_failure policy bookkeeping (Spec B §1.4 / §2.5):
        # ``_instance_to_spec`` -- per synthesized_id, the spawning FanOut id
        #   + the FanOutSpec that produced it. Lets the per-node result
        #   handler look up the spec's ``on_failure`` and the corresponding
        #   ``_fanout_drain_state`` entry.
        # ``_fanout_drain_state`` -- per ``f"{fanout_node_id}__{target_id}"``
        #   key (one FanOut may have multiple specs to different targets),
        #   tracks completed_count / any_failed / first_failure so the outer
        #   loop can decide at end-of-superstep whether to terminate
        #   ``failed`` (drain_then_fail) or continue (collect).
        self._instance_to_spec: dict[str, tuple[str, FanOutSpec]] = {}
        self._fanout_drain_state: dict[str, _FanoutDrainState] = {}
        # Phase 6 — mid-graph pause/resume bookkeeping (Spec B §2.3 step 3).
        # ``_pending_toolcalls`` accumulates ToolCall nodes that raised
        # :class:`YieldToWorker` during a superstep; the executor saves a
        # checkpoint, re-raises ``YieldToWorker`` upward (the worker parks
        # the session), and ``resume_from_checkpoint`` drains the list on
        # the resume path with ``bypass_approval=True``.
        self._pending_toolcalls: list[_PendingToolCall] = []
        # ``_pending_agent_yields`` accumulates agent nodes that raised
        # :class:`YieldToWorker` (ask_user / approval gate) during a
        # superstep; same park/checkpoint/resume flow as ToolCalls, but
        # resumed by rebuilding the node's agent turn (see
        # ``_resume_agent_node``).
        self._pending_agent_yields: list[_PendingAgentYield] = []
        # ``_context`` and ``_ready_set`` are populated by :meth:`invoke`
        # at the top of each superstep and kept on the executor so
        # :meth:`snapshot_state` can serialise them mid-flight. ``None``
        # before the first superstep / after termination.
        self._context: GraphContext | None = None
        self._ready_set: set[str] = set()
        # Node ids that have entered the ready set at least once this run.
        # Used by ``_fanin_ready`` to decide whether a callable-router source
        # is a *live* potential upstream (admitted but not yet resolved) that
        # a FanIn must wait for, vs. a branch that never activated (which must
        # NOT block the FanIn). Not part of the serialised checkpoint: a
        # callable-router source that already produced output no longer
        # blocks (its routing decision is settled), and resume only re-admits
        # nodes that are pending anyway.
        self._admitted: set[str] = set()
        self._node_states: dict[str, NodeRuntimeState] = {}

        # Turn-log emission. Subclasses (WorkspaceGraphExecutor /
        # GraphExecutor) override the factory and graph-level writer in
        # their __init__ to wire real backends; the base defaults emit
        # no-ops so legacy callers and graph unit tests run without
        # side effect.
        self._turn_log_factory: Callable[[str], TurnLogWriter] = (
            lambda node_id: NoopTurnLogWriter()
        )
        self._graph_turn_log: TurnLogWriter = NoopTurnLogWriter()
        # Per-node writers are cached for the lifetime of the executor
        # so a node that runs across multiple supersteps keeps writing
        # to the same monotonic seq stream. Without this cache, every
        # superstep restarts seq=1 -- breaks since_seq pagination and
        # collides StorageTurnLogWriter.id which embeds the seq.
        self._node_turn_logs: dict[str, TurnLogWriter] = {}
        # Set by `_run_superstep_loop` at each iteration boundary so
        # `_stream_node` can stamp the active superstep on its events.
        self._current_superstep_id: str | None = None

    @property
    def graph(self) -> Graph:
        return self._graph

    async def resume_from_checkpoint(
        self,
        checkpoint: dict[str, Any],
        *,
        resumed_tcid: str | None = None,
        agent_tool_result: "Message | None" = None,
    ) -> AsyncIterator[StreamEvent]:
        """Restore from a checkpoint and continue graph execution.

        ``resumed_tcid`` selects which pending human-interaction entry the
        human just replied to. When ``None`` (the legacy single-park /
        direct-test path) every pending ToolCall is drained at once. When
        set, only the matching entry is resumed (a pending ToolCall via
        bypass re-dispatch, or a pending agent-node yield via
        ``_resume_agent_node`` with ``agent_tool_result``); if other
        human-interaction nodes are still pending afterwards, the executor
        re-raises a :class:`YieldToWorker` so the worker re-parks on the
        remaining keys (drain-until-empty for full concurrency).

        Spec B §2.3 step 3 / Phase 6. Called by the worker after the
        operator approves a yielded ToolCall. The executor:

        1. Repopulates its mid-flight state via :meth:`restore_state`.
        2. Re-dispatches every pending ToolCall with ``bypass_approval=True``,
           via :meth:`_dispatch_toolcall_with_bypass` (so the approval
           gate doesn't fire again).
        3. Records each result into ``context.nodes`` (or stamps an
           error NodeOutput on failure / rejection / timeout).
        4. Marks the pending nodes ENDED in node_states.
        5. Computes the next ready set from the just-completed nodes and
           continues :meth:`_run_superstep_loop` to drain the rest of the
           graph.

        Subclasses that need to surface rejection/timeout exceptions
        from the worker (e.g. when the operator rejects the approval)
        should catch them in :meth:`_dispatch_toolcall_with_bypass` and
        re-raise as a domain exception the resume drain knows about —
        Phase 6 Task 6.4 handles this via the synthetic
        :class:`ToolApprovalRejected` exception.
        """
        self.restore_state(checkpoint)

        context = self._context
        if context is None:
            # Defence in depth: an empty / new checkpoint has no context;
            # treat as a no-op completion.
            return
        node_states = self._node_states
        ready = self._ready_set

        # Select which pending entries to resume this cycle. Legacy
        # (resumed_tcid None): every pending ToolCall. Per-entry: only the
        # matching tcid; the rest stay pending for the re-park below.
        tc_all = list(self._pending_toolcalls)
        ay_all = list(self._pending_agent_yields)
        if resumed_tcid is None:
            tc_pending = tc_all
            ay_pending = ay_all
        else:
            tc_pending = [e for e in tc_all if e.tool_call_id == resumed_tcid]
            ay_pending = [e for e in ay_all if e.tool_call_id == resumed_tcid]
        # Remove the resumed entries; keep the rest on the executor so the
        # re-park snapshot still carries them.
        self._pending_toolcalls = [e for e in tc_all if e not in tc_pending]
        self._pending_agent_yields = [e for e in ay_all if e not in ay_pending]
        completed_ids: list[str] = []
        for entry in tc_pending:
            node_def = self._resolve_node_def(entry.node_id)
            if not isinstance(node_def, _ToolCallNode):
                # Topology drifted between checkpoint + resume; surface
                # as a node failure so the outer loop terminates cleanly.
                node_states[entry.node_id] = NodeRuntimeState(
                    status=NodeRuntimeStatus.FAILED,
                    last_run_iteration=context.iteration,
                    last_run_at=datetime.now(timezone.utc),
                    error=(
                        f"resume: pending ToolCall node id {entry.node_id!r} "
                        f"resolves to {type(node_def).__name__!r}, not _ToolCallNode"
                    ),
                )
                completed_ids.append(entry.node_id)
                continue
            try:
                result = await self._dispatch_toolcall_with_bypass(
                    node_def, entry.arguments
                )
            except YieldToWorker as yld:
                # Two-phase park: the approval gate sat on a *yielding* tool.
                # The operator APPROVED (phase 1), so the bypassed re-dispatch
                # ran the real tool - which itself yields for its own event
                # (timer/file/graph/human). Do NOT swallow this as a node
                # failure: re-record the node as a pending ToolCall on the
                # NEW event key (phase 2) so the drain-until-empty check below
                # re-parks via _build_pending_park_yield(). Mirrors the normal
                # dispatch path's YieldToWorker handling in _stream_node.
                self._pending_toolcalls.append(
                    _PendingToolCall(
                        node_id=entry.node_id,
                        tool_call_id=yld.tool_call_id,
                        parked_event_key=yld.yielded.event_key,
                        arguments=entry.arguments,
                    )
                )
                continue
            except _ToolApprovalRejected as rej:
                # Spec B §4.8 / Phase 6 Task 6.4 — operator rejected or
                # the approval timed out; stamp the node as a failure
                # with ``ended_detail='tool_execution_failed'``.
                fail_out = NodeOutput(
                    text="",
                    parsed=None,
                    history=[],
                    iteration=context.iteration,
                    error=str(rej),
                    ended_detail="tool_execution_failed",
                )
                context.nodes[entry.node_id] = fail_out
                node_states[entry.node_id] = NodeRuntimeState(
                    status=NodeRuntimeStatus.FAILED,
                    last_run_iteration=context.iteration,
                    last_run_at=datetime.now(timezone.utc),
                    error=str(rej),
                )
                # Spec B §4.8 — emit a terminal error event so taps see
                # the rejection, then mark the graph failed.
                yield _GraphErrorEvent(  # type: ignore[misc]
                    code="tool_execution_failed",
                    message=str(rej),
                    node_id=entry.node_id,
                )
                await self._save_state(
                    iteration=context.iteration,
                    node_states=node_states,
                    status=SessionStatus.ENDED,
                    ended_reason="failed",
                    ended_detail="tool_execution_failed",
                )
                return
            except Exception as exc:  # noqa: BLE001 -- map all to node failure
                fail_out = NodeOutput(
                    text="",
                    parsed=None,
                    history=[],
                    iteration=context.iteration,
                    error=str(exc),
                    ended_detail="tool_execution_failed",
                )
                context.nodes[entry.node_id] = fail_out
                node_states[entry.node_id] = NodeRuntimeState(
                    status=NodeRuntimeStatus.FAILED,
                    last_run_iteration=context.iteration,
                    last_run_at=datetime.now(timezone.utc),
                    error=str(exc),
                )
                yield _GraphErrorEvent(  # type: ignore[misc]
                    code="tool_execution_failed",
                    message=str(exc),
                    node_id=entry.node_id,
                )
                await self._save_state(
                    iteration=context.iteration,
                    node_states=node_states,
                    status=SessionStatus.ENDED,
                    ended_reason="failed",
                    ended_detail="tool_execution_failed",
                )
                return
            # Map the result through the same path as the normal
            # _stream_node ToolCall handler so schema-validation failures
            # surface consistently.
            mapped = _map_toolcall_result(
                result, output_schema=node_def.output_schema
            )
            if mapped.error_code is not None:
                fail_out = NodeOutput(
                    text=mapped.text,
                    parsed=None,
                    history=[],
                    iteration=context.iteration,
                    error=mapped.error_message or mapped.error_code,
                    ended_detail=mapped.error_code,
                )
                context.nodes[entry.node_id] = fail_out
                node_states[entry.node_id] = NodeRuntimeState(
                    status=NodeRuntimeStatus.FAILED,
                    last_run_iteration=context.iteration,
                    last_run_at=datetime.now(timezone.utc),
                    error=mapped.error_message or mapped.error_code,
                )
                yield _GraphErrorEvent(  # type: ignore[misc]
                    code=mapped.error_code,
                    message=mapped.error_message or mapped.error_code,
                    node_id=entry.node_id,
                )
                await self._save_state(
                    iteration=context.iteration,
                    node_states=node_states,
                    status=SessionStatus.ENDED,
                    ended_reason="failed",
                    ended_detail=mapped.error_code,
                )
                return
            tc_out = NodeOutput(
                text=mapped.text,
                parsed=mapped.parsed,
                history=[],
                iteration=context.iteration,
            )
            context.nodes[entry.node_id] = tc_out
            node_states[entry.node_id] = NodeRuntimeState(
                status=NodeRuntimeStatus.ENDED,
                last_run_iteration=context.iteration,
                last_run_at=datetime.now(timezone.utc),
            )
            completed_ids.append(entry.node_id)

        # Resume the selected agent-node yields: continue each node's turn
        # with the human's answer / decision injected as the tool result.
        for ay in ay_pending:
            try:
                out = await self._resume_agent_node(
                    ay, agent_tool_result if agent_tool_result is not None
                    else Message(role="tool", parts=[
                        ToolResultPart(id=ay.tool_call_id, output="")]),
                )
            except Exception as exc:  # noqa: BLE001 -- map to node failure
                fail_out = NodeOutput(
                    text="", parsed=None, history=[],
                    iteration=context.iteration, error=str(exc),
                    ended_detail="tool_execution_failed",
                )
                context.nodes[ay.node_id] = fail_out
                node_states[ay.node_id] = NodeRuntimeState(
                    status=NodeRuntimeStatus.FAILED,
                    last_run_iteration=context.iteration,
                    last_run_at=datetime.now(timezone.utc),
                    error=str(exc),
                )
                yield _GraphErrorEvent(  # type: ignore[misc]
                    code="tool_execution_failed", message=str(exc),
                    node_id=ay.node_id,
                )
                await self._save_state(
                    iteration=context.iteration, node_states=node_states,
                    status=SessionStatus.ENDED, ended_reason="failed",
                    ended_detail="tool_execution_failed",
                )
                return
            context.nodes[ay.node_id] = out
            node_states[ay.node_id] = NodeRuntimeState(
                status=NodeRuntimeStatus.ENDED,
                last_run_iteration=context.iteration,
                last_run_at=datetime.now(timezone.utc),
            )
            completed_ids.append(ay.node_id)

        # Full concurrency: if other human-interaction nodes are still
        # pending (the human has only replied to some), re-park on the
        # remaining keys instead of advancing the graph.
        if self._pending_toolcalls or self._pending_agent_yields:
            await self._save_state(
                iteration=context.iteration,
                node_states=node_states,
                status=SessionStatus.WAITING,
            )
            raise self._build_pending_park_yield()

        # Persist the drained-state snapshot so observers can see the
        # ToolCalls finished before the next superstep starts.
        await self._save_state(
            iteration=context.iteration,
            node_states=node_states,
            status=SessionStatus.RUNNING,
        )

        # Compute the next ready set from the now-completed pending
        # ToolCall nodes. The ``ready`` set on the executor at the time
        # of the yield was the set of in-flight nodes; the just-completed
        # subset is ``completed_ids`` (the others, if any, already had
        # their results applied before the yield fired).
        if completed_ids:
            try:
                next_ready = await self._compute_next_ready(
                    set(completed_ids), context
                )
            except _RoutingFailed as exc:
                yield _GraphErrorEvent(  # type: ignore[misc]
                    code="routing_failed",
                    message=str(exc),
                    node_id=exc.source_node_id,
                )
                await self._save_state(
                    iteration=context.iteration,
                    node_states=node_states,
                    status=SessionStatus.ENDED,
                    ended_reason="failed",
                    ended_detail="routing_failed",
                )
                return
            # Drain any fan-out plans spawned by the drained ToolCalls.
            for fanout_id, instances in list(self._pending_fanout.items()):
                for inst in instances:
                    self._fanout_instances[inst.synthesized_id] = inst
                    next_ready.add(inst.synthesized_id)
                del self._pending_fanout[fanout_id]
            context.iteration += 1
            ready = next_ready
            self._ready_set = ready

        async for ev in self._run_superstep_loop(
            context=context,
            node_states=node_states,
            ready=ready,
            ended_reason_in=None,
            ended_detail_in=None,
        ):
            yield ev

    # ---- Subclass hooks --------------------------------------------------

    @abstractmethod
    async def _load_node_history(self, node_id: str) -> list[Message]:
        """Return the accumulated message history for ``node_id``."""

    @abstractmethod
    async def _persist_node_turn(
        self,
        node_id: str,
        iteration: int,
        new_messages: list[Message],
    ) -> None:
        """Append ``new_messages`` to the node's history."""

    @abstractmethod
    async def _save_state(
        self,
        *,
        iteration: int,
        node_states: dict[str, NodeRuntimeState],
        status: SessionStatus,
        ended_reason: str | None = None,
        ended_detail: str | None = None,
    ) -> None:
        """Persist graph-level state between supersteps."""

    async def _build_sub_executor(
        self,
        parent_node: _GraphNodeRef,
        sub_graph: Graph,
    ) -> "_BaseGraphExecutor":
        """Build a child executor for a subgraph node.

        Default raises :class:`ConfigError`; concrete classes that
        support subgraph composition override this.
        """
        raise ConfigError(
            f"subgraph node {parent_node.id!r} not supported by "
            f"{type(self).__name__}: subclass must override _build_sub_executor"
        )

    async def _dispatch_toolcall(
        self,
        node: "_ToolCallNode",
        arguments: dict[str, Any],
    ) -> "ToolResultPart":
        """Dispatch a ToolCall node's tool. Default raises NotImplementedError.

        Concrete executors override:

        * :class:`primer.graph.WorkspaceGraphExecutor` wires the workspace
          session's :class:`ToolExecutionManager`.
        * :class:`primer.graph.GraphExecutor` exposes a
          ``tool_dispatcher`` constructor arg for tests / non-workspace
          callers that supply their own dispatch surface.
        """
        raise NotImplementedError(
            f"_dispatch_toolcall must be overridden to invoke tool "
            f"{node.tool_id!r}"
        )

    async def _dispatch_toolcall_with_bypass(
        self,
        node: "_ToolCallNode",
        arguments: dict[str, Any],
    ) -> "ToolResultPart":
        """Re-dispatch a previously-yielded ToolCall with ``bypass_approval=True``.

        Spec B §2.3 step 3 / Phase 6 — invoked by
        :meth:`resume_from_checkpoint` to drain pending ToolCalls after
        operator approval, skipping the approval gate so the tool's
        underlying handler runs directly.

        Default implementation falls back to :meth:`_dispatch_toolcall`
        (no bypass) — subclasses with a real approval-aware dispatch
        surface should override to thread ``bypass_approval=True``
        through to their underlying manager.
        """
        return await self._dispatch_toolcall(node, arguments)

    # ---- Public surface --------------------------------------------------

    async def invoke(
        self,
        messages: "list[Message] | Any",
    ) -> AsyncIterator[StreamEvent]:
        """Execute the graph; stream events live as they happen.

        Concurrent nodes within a superstep stream their events
        through a shared :class:`asyncio.Queue`; the caller sees
        events in arrival order, interleaved across nodes. Each
        event is wrapped in
        :class:`ExtendedEvent(_GraphNodeEvent(...))` carrying the
        ``node_id`` and ``iteration`` so consumers can demultiplex.

        ``messages`` historically was a ``list[Message]``; spec §4.3
        widens the input to ``Any`` so callers (e.g. the workspace
        executor relaying ``session.metadata['graph_input']``) can pass
        dict / str / list / any JSON-serialisable value. Begin-firing
        branches on the runtime type to shape the right NodeOutput.
        """
        node_states: dict[str, NodeRuntimeState] = {
            n.id: NodeRuntimeState(status=NodeRuntimeStatus.PENDING)
            for n in self._graph.nodes
        }
        # Preserve non-list inputs verbatim so Begin can materialise its
        # NodeOutput from dict / str / Any.
        if isinstance(messages, list):
            initial_input: Any = list(messages)
        else:
            initial_input = messages
        context = GraphContext(
            initial_input=initial_input,
            iteration=0,
            nodes={},
        )
        ready: set[str] = {_resolve_initial_ready_node(self._graph)}
        self._admitted = set(ready)
        ended_reason: str | None = None
        ended_detail: str | None = None
        # Phase 6 — expose mid-flight state on the executor so
        # :meth:`snapshot_state` can capture it the moment a ToolCall
        # yields for approval. Kept in sync after every superstep
        # boundary (and after applying per-node results).
        self._context = context
        self._ready_set = ready
        self._node_states = node_states

        async for ev in self._run_superstep_loop(
            context=context,
            node_states=node_states,
            ready=ready,
            ended_reason_in=ended_reason,
            ended_detail_in=ended_detail,
        ):
            yield ev

    async def _run_superstep_loop(
        self,
        *,
        context: GraphContext,
        node_states: dict[str, NodeRuntimeState],
        ready: set[str],
        ended_reason_in: str | None,
        ended_detail_in: str | None,
    ) -> AsyncIterator[StreamEvent]:
        """Pregel superstep loop — extracted so :meth:`resume_from_checkpoint`
        can re-enter at the same point after the approval-yield round-trip.

        Spec B §2.3 step 3 / Phase 6. ``ready``, ``context``, ``node_states``
        are passed by reference (the executor's instance attrs hold the same
        objects) so the snapshot path always sees up-to-date state.
        """
        ended_reason = ended_reason_in
        ended_detail = ended_detail_in

        while ready:
            # Cycle bound check. Spec §5.4 maps this to ended_reason='failed'
            # with the detail code carried separately so the public contract
            # has a single failure reason and a finite set of codes.
            if (
                self._graph.max_iterations is not None
                and context.iteration >= self._graph.max_iterations
            ):
                yield _GraphErrorEvent(  # type: ignore[misc]
                    code="max_iterations_exceeded",
                    message=f"graph ran for {context.iteration} iterations",
                    node_id=None,
                )
                ended_reason = "failed"
                ended_detail = "max_iterations_exceeded"
                break

            # Mark all ready nodes RUNNING and snapshot state.
            for nid in ready:
                node_states[nid] = NodeRuntimeState(
                    status=NodeRuntimeStatus.RUNNING,
                    last_run_iteration=context.iteration,
                    last_run_at=datetime.now(timezone.utc),
                )
            await self._save_state(
                iteration=context.iteration,
                node_states=node_states,
                status=SessionStatus.RUNNING,
            )

            # Emit superstep_started + open per-node writers. Stamped on
            # ``self._current_superstep_id`` so child node writers can
            # carry the same id.
            superstep_id = f"ss-{context.iteration}-{uuid.uuid4().hex[:6]}"
            self._current_superstep_id = superstep_id
            ss_started_at = datetime.now(timezone.utc)
            await _safe_graph_turn_log(
                self._graph_turn_log,
                TurnLogSuperstepStarted(
                    seq=0,
                    ts=ss_started_at,
                    iteration=context.iteration,
                    superstep_id=superstep_id,
                    ready_node_ids=sorted(ready),
                ),
            )

            # Run all ready nodes concurrently. Each pushes its events
            # live to the shared queue; we drain them as they arrive
            # and yield to the caller. _NodeDone sentinels track
            # completion + per-node final result.
            #
            # Spec B §2.4 — Multi-End independent termination: every
            # End fires independently when reached. There is no
            # "first End wins" / "lex-smallest tie-break" any more.
            # The outer loop terminates naturally when the ready set
            # drains AND no nodes are in-flight; the sort here is kept
            # purely for deterministic stream ordering.
            ready_ordered = sorted(ready)
            queue: "asyncio.Queue[StreamEvent | _NodeDone]" = asyncio.Queue()

            # Per-node turn-log writers + start timestamps. Writers
            # are cached on self._node_turn_logs so a node that fires
            # in multiple supersteps keeps the same monotonic seq
            # stream; the cache miss path is the only one that calls
            # the factory. Closing is deferred to end-of-run.
            node_started_at: dict[str, datetime] = {}
            for nid in ready_ordered:
                w = self._node_turn_logs.get(nid)
                if w is None:
                    w = self._turn_log_factory(nid)
                    self._node_turn_logs[nid] = w
                started_at = datetime.now(timezone.utc)
                node_started_at[nid] = started_at
                await _safe_graph_turn_log(
                    w,
                    TurnLogStarted(
                        seq=0,
                        ts=started_at,
                        node_id=nid,
                        iteration=context.iteration,
                        superstep_id=superstep_id,
                        model=None,
                        input_message_count=0,
                    ),
                )

            tasks: list[asyncio.Task] = [
                asyncio.create_task(
                    self._stream_node(nid, context, queue)
                )
                for nid in ready_ordered
            ]
            results: dict[str, _NodeDone] = {}
            done_count = 0
            try:
                while done_count < len(tasks):
                    item = await queue.get()
                    if isinstance(item, _NodeDone):
                        results[item.node_id] = item
                        done_count += 1
                        # Emit completed / failed for this node as soon
                        # as its _NodeDone lands. Skip emission for the
                        # suspended sentinel (approval-yield path,
                        # Spec B §2.3 step 3): the node is parked, not
                        # done; its real completion event lands on the
                        # resume-from-checkpoint path.
                        if item.suspended:
                            continue
                        w = self._node_turn_logs.get(item.node_id)
                        started_at = node_started_at.get(
                            item.node_id, datetime.now(timezone.utc),
                        )
                        if w is not None:
                            duration_ms = max(
                                0,
                                int((
                                    datetime.now(timezone.utc) - started_at
                                ).total_seconds() * 1000),
                            )
                            if item.error is None:
                                await _safe_graph_turn_log(
                                    w,
                                    TurnLogCompleted(
                                        seq=0,
                                        ts=datetime.now(timezone.utc),
                                        node_id=item.node_id,
                                        iteration=context.iteration,
                                        superstep_id=superstep_id,
                                        duration_ms=duration_ms,
                                    ),
                                )
                            else:
                                # Two shapes reach here: a real
                                # BaseException (line ~2112) or a
                                # pre-stringified error (FanOut /
                                # template / End nodes). Route the
                                # former through to_problem_details so
                                # NetworkError -> 504, AuthenticationError
                                # -> 401, etc., land in the UI; keep
                                # the latter wrapped in a generic 500.
                                if isinstance(item.error, BaseException):
                                    error_envelope = to_problem_details(
                                        item.error,
                                    )
                                    if item.ended_detail and (
                                        error_envelope.extensions is not None
                                    ):
                                        error_envelope.extensions[
                                            "ended_detail"
                                        ] = item.ended_detail
                                else:
                                    error_envelope = ProblemDetails(
                                        type="/errors/graph-node-failed",
                                        title="Graph node failed",
                                        status=500,
                                        detail=str(item.error),
                                        extensions={
                                            "ended_detail": (
                                                item.ended_detail
                                            ),
                                        },
                                    )
                                await _safe_graph_turn_log(
                                    w,
                                    TurnLogFailed(
                                        seq=0,
                                        ts=datetime.now(timezone.utc),
                                        node_id=item.node_id,
                                        iteration=context.iteration,
                                        superstep_id=superstep_id,
                                        duration_ms=duration_ms,
                                        error=error_envelope,
                                    ),
                                )
                    else:
                        yield item
            finally:
                # Belt-and-braces: tasks should all have completed by now,
                # but if the caller closed the iterator early, cancel.
                for t in tasks:
                    if not t.done():
                        t.cancel()
                for t in tasks:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        # Swallow cancellation (we cancelled them) and any
                        # task-body error during cleanup, but let
                        # SystemExit / KeyboardInterrupt / GeneratorExit
                        # propagate so the process can shut down cleanly.
                        pass
                # Emit superstep_ended + close per-node writers.
                # Happens BEFORE the per-node results loop / termination
                # decisions so the graph-level log captures every
                # superstep even on a break that follows.
                completed_node_ids = sorted(
                    nid for nid, d in results.items()
                    if d.error is None and not d.suspended
                )
                failed_node_ids = sorted(
                    nid for nid, d in results.items()
                    if d.error is not None
                )
                await _safe_graph_turn_log(
                    self._graph_turn_log,
                    TurnLogSuperstepEnded(
                        seq=0,
                        ts=datetime.now(timezone.utc),
                        iteration=context.iteration,
                        superstep_id=superstep_id,
                        completed_node_ids=completed_node_ids,
                        failed_node_ids=failed_node_ids,
                        duration_ms=max(
                            0,
                            int((
                                datetime.now(timezone.utc) - ss_started_at
                            ).total_seconds() * 1000),
                        ),
                    ),
                )
                # Per-node writers stay open across supersteps -- they
                # are closed by _close_turn_logs() when the run ends.
                self._current_superstep_id = None

            # Apply per-node results to the supersteps' node_states +
            # graph context, decide whether to terminate.
            #
            # Spec B §2.4 — End nodes no longer short-circuit the
            # outer loop; they only emit their _GraphEndOutputEvent and
            # produce a NodeOutput. The loop terminates when the ready
            # set drains naturally (the `while ready:` predicate).
            any_failed = False
            # Error events to yield AFTER the per-node loop (yielding
            # inside the loop while we mutate node_states would be fine,
            # but emitting once we've classified everything keeps the
            # ordering predictable: results first, then the terminal error).
            error_events: list[_GraphErrorEvent] = []
            for nid in ready_ordered:
                done = results.get(nid)
                # Spec B §2.3 step 3 / Phase 6 — a suspended ToolCall has
                # already been recorded into ``_pending_toolcalls``; leave
                # its status as RUNNING (set above by the pre-run snapshot)
                # and skip context updates. The post-superstep block
                # detects pending entries and propagates YieldToWorker.
                if done is not None and done.suspended:
                    continue
                if done is None or done.error is not None:
                    err_text = (
                        str(done.error) if done is not None else "no result"
                    )
                    node_states[nid] = NodeRuntimeState(
                        status=NodeRuntimeStatus.FAILED,
                        last_run_iteration=context.iteration,
                        last_run_at=datetime.now(timezone.utc),
                        error=err_text,
                    )
                    # Spec B §2.5 — when this failed node is a fan-out
                    # instance whose spawning spec has on_failure != fail_fast,
                    # SUPPRESS the immediate failure: bump the drain state and
                    # let the superstep continue. For ``collect`` we also
                    # stamp NodeOutput.error and append to the aggregator.
                    inst_spec = self._instance_to_spec.get(nid)
                    if inst_spec is not None and inst_spec[1].on_failure != "fail_fast":
                        fanout_id, spec = inst_spec
                        drain_key = f"{fanout_id}__{spec.target_node_id or ''}"
                        # Fall back to scanning when the spec is a tee (target
                        # is in target_node_ids and nid identifies one of them).
                        if drain_key not in self._fanout_drain_state:
                            # Recover the target id from the synthesized id's
                            # _FanoutInstance entry (tee path: synthesized_id ==
                            # target_node_id; broadcast/map: target_node_id is
                            # the bare target).
                            inst_lookup = self._fanout_instances.get(nid)
                            if inst_lookup is not None:
                                drain_key = (
                                    f"{fanout_id}__{inst_lookup.target_node_id}"
                                )
                        drain = self._fanout_drain_state.get(drain_key)
                        if drain is not None:
                            drain.completed_count += 1
                            drain.any_failed = True
                            if drain.first_failure is None:
                                drain.first_failure = (
                                    nid, done.ended_detail or "node_failed",
                                )
                            # ``collect`` mode: stamp NodeOutput.error so
                            # downstream FanIn templates can branch on n.error,
                            # and append to the aggregator list so the FanIn
                            # ready-set still counts the failed instance.
                            if spec.on_failure == "collect":
                                fail_output = NodeOutput(
                                    text="",
                                    parsed=None,
                                    history=[],
                                    iteration=context.iteration,
                                    error=(
                                        str(done.error)
                                        if done is not None and done.error is not None
                                        else (done.ended_detail if done else "node_failed")
                                    ),
                                    ended_detail=(
                                        done.ended_detail
                                        if done is not None and done.ended_detail is not None
                                        else "node_failed"
                                    ),
                                )
                                context.nodes[nid] = fail_output
                                inst_obj = self._fanout_instances.get(nid)
                                if inst_obj is not None:
                                    agg = context.nodes.get(inst_obj.target_node_id)
                                    agg_list: list[NodeOutput | None] = (
                                        list(agg) if isinstance(agg, list) else []  # type: ignore[arg-type]
                                    )
                                    if inst_obj.fanout_index is not None:
                                        target_len = inst_obj.fanout_index + 1
                                        while len(agg_list) < target_len:
                                            agg_list.append(None)
                                        agg_list[inst_obj.fanout_index] = fail_output
                                    else:
                                        # tee: append (no leading-None pad).
                                        agg_list.append(fail_output)
                                    # Keep the list positionally aligned: a slot
                                    # that has not reported yet stays ``None`` at
                                    # its own index. Compacting here would shift
                                    # later results onto the wrong index and make
                                    # the FanIn ready-set undercount. The FanIn
                                    # template / consumer reads by position.
                                    context.nodes[inst_obj.target_node_id] = (
                                        agg_list
                                    )
                            continue
                    any_failed = True
                    # When the failure carries a spec §5.4 code (e.g. End-node
                    # output validation), propagate it so the executor's
                    # final state records both reason="failed" and the code,
                    # and emit a terminal _GraphErrorEvent for taps to see.
                    if done is not None and done.ended_detail is not None:
                        ended_reason = "failed"
                        ended_detail = done.ended_detail
                        error_events.append(
                            _GraphErrorEvent(
                                code=done.ended_detail,
                                message=err_text,
                                node_id=nid,
                            )
                        )
                    continue
                if done.output is not None:
                    context.nodes[nid] = done.output
                    # Fan-out instance: also append to the aggregator list at
                    # the target's bare id, preserving index order via
                    # pad-with-None for out-of-order completion.
                    inst = self._fanout_instances.get(nid)
                    if inst is not None:
                        agg = context.nodes.get(inst.target_node_id)
                        agg_list = list(agg) if isinstance(agg, list) else []  # type: ignore[arg-type]
                        if inst.fanout_index is not None:
                            # Indexed (broadcast/map): place at its own slot,
                            # padding with None for out-of-order completion.
                            target_len = inst.fanout_index + 1
                            while len(agg_list) < target_len:
                                agg_list.append(None)
                            agg_list[inst.fanout_index] = done.output
                        else:
                            # tee: one run per named target -> just append.
                            # Padding here would leave a leading None
                            # (nodes.<target>[0] == None).
                            agg_list.append(done.output)
                        # Keep the list positionally aligned (see the collect
                        # path above): an instance that has not reported stays
                        # ``None`` at its index; never compact, or later
                        # results shift onto the wrong index.
                        context.nodes[inst.target_node_id] = agg_list
                        # Spec B §2.5 — count successful instances against the
                        # drain state too so drain_then_fail / collect modes
                        # know when every sibling has reported in.
                        inst_spec_ok = self._instance_to_spec.get(nid)
                        if inst_spec_ok is not None:
                            fanout_id_ok, spec_ok = inst_spec_ok
                            drain_key_ok = (
                                f"{fanout_id_ok}__{inst.target_node_id}"
                            )
                            drain_ok = self._fanout_drain_state.get(drain_key_ok)
                            if drain_ok is not None:
                                drain_ok.completed_count += 1
                node_states[nid] = NodeRuntimeState(
                    status=NodeRuntimeStatus.ENDED,
                    last_run_iteration=context.iteration,
                    last_run_at=datetime.now(timezone.utc),
                )

            # Persist superstep results so each turn-end is recoverable
            # (and, in the workspace executor, git-committed).
            await self._save_state(
                iteration=context.iteration,
                node_states=node_states,
                status=SessionStatus.RUNNING,
            )

            # Spec B §2.3 step 3 / Phase 6 — if any ToolCall(s) yielded
            # for approval this superstep, save the checkpoint via the
            # subclass hook + raise YieldToWorker so the worker can park
            # the session. The first pending entry's parked_event_key
            # becomes the wake-up key; subsequent entries are drained on
            # the resume path. ``_save_state`` is called with
            # ``SessionStatus.WAITING`` so the persisted state reflects
            # the paused world.
            if self._pending_toolcalls or self._pending_agent_yields:
                # Keep ready set / context up to date on the executor for
                # the snapshot.
                self._ready_set = set(ready_ordered)
                self._context = context
                self._node_states = node_states
                await self._save_state(
                    iteration=context.iteration,
                    node_states=node_states,
                    status=SessionStatus.WAITING,
                )
                # Park on the full human-interaction set (tool_call
                # approvals + agent-node yields). Any one firing wakes the
                # session, which resumes that node and re-parks on the rest.
                raise self._build_pending_park_yield()

            if any_failed:
                # ended_reason / ended_detail may already be set by the
                # per-node handler above (e.g. End-node failure carries a
                # spec §5.4 code); only fall back when nothing's been set.
                if ended_reason is None:
                    ended_reason = "failed"
                for ev in error_events:
                    yield ev  # type: ignore[misc]
                break

            # Spec B §2.5 — drain_then_fail: once every instance for a
            # (FanOut, target) pair has reported, if any failed terminate
            # the graph ``failed`` with ``fanin_upstream_failed``. We check
            # every drain state because multiple FanOut specs can target
            # different ids; the first one that signals failure wins.
            drain_failure: tuple[str, str] | None = None
            for drain in self._fanout_drain_state.values():
                if (
                    drain.on_failure == "drain_then_fail"
                    and drain.any_failed
                    and drain.completed_count >= drain.expected_count
                    and drain.first_failure is not None
                ):
                    drain_failure = drain.first_failure
                    break
            if drain_failure is not None:
                failed_nid, _failed_detail = drain_failure
                yield _GraphErrorEvent(  # type: ignore[misc]
                    code="fanin_upstream_failed",
                    message=(
                        f"fan-out worker {failed_nid!r} failed; aborting "
                        "after draining sibling workers"
                    ),
                    node_id=failed_nid,
                )
                ended_reason = "failed"
                ended_detail = "fanin_upstream_failed"
                break

            # Compute next ready set by evaluating outgoing edges.
            try:
                next_ready = await self._compute_next_ready(
                    set(ready_ordered), context
                )
            except _RoutingFailed as exc:
                logger.warning(
                    "GraphExecutor: routing failed",
                    extra={
                        "graph_id": self._graph.id,
                        "node_id": exc.source_node_id,
                        "error": str(exc),
                    },
                )
                yield _GraphErrorEvent(  # type: ignore[misc]
                    code="routing_failed",
                    message=str(exc),
                    node_id=exc.source_node_id,
                )
                ended_reason = "failed"
                ended_detail = "routing_failed"
                break
            except ConfigError as exc:
                logger.warning(
                    "GraphExecutor: edge evaluation failed",
                    extra={"graph_id": self._graph.id, "error": str(exc)},
                )
                ended_reason = "failed"
                break

            # Drain any pending fan-out plans into the next-ready set.
            # Spec B §2.1 — each synthesized instance becomes a node in the
            # next superstep; per-instance dispatch resolves the underlying
            # target node and renders its template with fanout_* in scope
            # (Task 2.4).
            for fanout_id, instances in list(self._pending_fanout.items()):
                for inst in instances:
                    self._fanout_instances[inst.synthesized_id] = inst
                    next_ready.add(inst.synthesized_id)
                del self._pending_fanout[fanout_id]

            ready = next_ready
            context.iteration += 1

        if ended_reason is None:
            ended_reason = "completed"

        await self._save_state(
            iteration=context.iteration,
            node_states=node_states,
            status=SessionStatus.ENDED,
            ended_reason=ended_reason,
            ended_detail=ended_detail,
        )

        # Close every per-node writer + the graph-level writer now that
        # the run has ended. A subsequent invoke() on the same executor
        # is not supported (graph executors are single-shot), so this
        # is safe to do here rather than in __aexit__-style cleanup.
        await self._close_turn_logs()

    async def _close_turn_logs(self) -> None:
        """Close every node + graph-level turn-log writer. Idempotent."""
        for w in list(self._node_turn_logs.values()):
            try:
                await w.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("turn_log aclose failed; continuing")
        self._node_turn_logs.clear()
        try:
            await self._graph_turn_log.aclose()
        except Exception:  # noqa: BLE001
            logger.exception("graph turn_log aclose failed; continuing")

    # ---- Per-node streaming ----------------------------------------------

    def _resolve_node_def(self, node_id: str):
        """Resolve ``node_id`` to its node definition.

        Synthesized fan-out instance ids (e.g. ``"worker[2]"``) resolve to
        their target node's definition; all other ids resolve directly via
        ``_nodes_by_id``. Spec B §2.1.
        """
        instance = self._fanout_instances.get(node_id)
        if instance is not None:
            return self._nodes_by_id[instance.target_node_id]
        return self._nodes_by_id[node_id]

    async def _stream_node(
        self,
        node_id: str,
        context: GraphContext,
        queue: "asyncio.Queue[StreamEvent | _NodeDone]",
    ) -> None:
        """Run one node; push events live to ``queue``, then a _NodeDone.

        Spec B §2.1: synthesized fan-out instance ids (``worker[2]`` etc.)
        resolve to the underlying target node definition, and the executor
        renders the node's input_template against a Jinja scope that includes
        ``fanout_index`` and ``fanout_item``.
        """
        instance = self._fanout_instances.get(node_id)
        if instance is not None:
            node = self._nodes_by_id[instance.target_node_id]
            extra_scope: dict[str, Any] | None = {
                "fanout_index": instance.fanout_index,
                "fanout_item": instance.fanout_item,
            }
        else:
            node = self._nodes_by_id[node_id]
            extra_scope = None
        try:
            if isinstance(node, _FanOutNode):
                # FanOut is a pure dispatcher (Spec B §2.1):
                # 1) Build its own bookkeeping NodeOutput.
                # 2) Resolve every spec into instances.
                # 3) Stash the instance plan on the executor so the outer
                #    superstep loop drains them into next_ready.
                fanout_self_output = NodeOutput(
                    text=json.dumps(
                        {"node_id": node.id, "specs": len(node.specs)}
                    ),
                    parsed=None,
                    history=[],
                    iteration=context.iteration,
                )
                try:
                    all_instances: list[_FanoutInstance] = []
                    # Track which spec each instance came from so the per-node
                    # result handler can look up ``on_failure`` (Spec B §2.5).
                    instance_specs: list[tuple[_FanoutInstance, FanOutSpec]] = []
                    for spec in node.specs:
                        spec_insts = _resolve_fanout_spec(
                            spec, context, fanout_self_output
                        )
                        all_instances.extend(spec_insts)
                        for inst in spec_insts:
                            instance_specs.append((inst, spec))
                except _FanoutSourceInvalid as exc:
                    await queue.put(
                        _NodeDone(
                            node_id=node.id,
                            output=None,
                            error=exc.reason,
                            ended_detail="fanout_source_invalid",
                        )
                    )
                    return
                # Record the FanOut's own NodeOutput so downstream conditional
                # edges from FanOut can read it.
                await queue.put(
                    _NodeDone(
                        node_id=node.id,
                        output=fanout_self_output,
                        error=None,
                    )
                )
                # Stash plan + per-target expected counts for FanIn ready-set.
                self._pending_fanout[node.id] = all_instances
                counts: dict[str, int] = {}
                for inst in all_instances:
                    counts[inst.target_node_id] = (
                        counts.get(inst.target_node_id, 0) + 1
                    )
                for tgt, n in counts.items():
                    self._fanout_target_expected_count[tgt] = max(
                        self._fanout_target_expected_count.get(tgt, 0), n
                    )
                # Spec B §2.5 — populate per-instance spec lookup + per-(FanOut,
                # target) drain state so the outer loop's result-application
                # path can branch on ``on_failure``.
                #
                # When a spec is "fail_fast" we still register it so the
                # per-node handler's lookup is consistent; the handler only
                # consults the drain state for non-fail_fast modes, so the
                # bookkeeping cost is one tuple per instance.
                for inst, spec in instance_specs:
                    self._instance_to_spec[inst.synthesized_id] = (
                        node.id, spec,
                    )
                # Build / refresh one drain-state entry per (fanout, target).
                # Key uses '__' separator since target ids are normal identifiers.
                spec_target_counts: dict[tuple[str, str], int] = {}
                spec_by_target: dict[tuple[str, str], FanOutSpec] = {}
                for inst, spec in instance_specs:
                    key = (node.id, inst.target_node_id)
                    spec_target_counts[key] = (
                        spec_target_counts.get(key, 0) + 1
                    )
                    # All instances for one (fanout, target) belong to the
                    # same FanOutSpec by construction (each spec emits to
                    # exactly one target id for broadcast/map; tee writes
                    # one instance per target).
                    spec_by_target[key] = spec
                for (fanout_id, target_id), expected in spec_target_counts.items():
                    drain_key = f"{fanout_id}__{target_id}"
                    spec = spec_by_target[(fanout_id, target_id)]
                    self._fanout_drain_state[drain_key] = _FanoutDrainState(
                        on_failure=spec.on_failure,
                        fanout_node_id=fanout_id,
                        target_node_id=target_id,
                        expected_count=expected,
                    )
                return
            if isinstance(node, _FanInNode):
                # FanIn is a pure data-shaping aggregator (Spec B §2.2):
                # render aggregate_template + optional output_schema, then
                # post a _NodeDone with ended_detail set on failure so the
                # outer loop terminates `failed`.
                fres = _render_fanin_output(node, context)
                if fres.error_code is not None:
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=fres.error_message or fres.error_code,
                            ended_detail=fres.error_code,
                        )
                    )
                    return
                fan_out = NodeOutput(
                    text=fres.text,
                    parsed=fres.parsed,
                    history=[],
                    iteration=context.iteration,
                )
                await queue.put(
                    _NodeDone(node_id=node_id, output=fan_out, error=None)
                )
                return
            if isinstance(node, _EndNode):
                # End is pure data-shaping; render output_template + optional
                # schema validation, then post a _NodeDone with ended_detail
                # set on failure so the outer loop terminates `failed`.
                res = _render_end_output(node, context)
                if res.error_code is not None:
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=res.error_message or res.error_code,
                            ended_detail=res.error_code,
                        )
                    )
                    return
                out = NodeOutput(
                    text=res.text,
                    parsed=res.parsed,
                    history=[],
                    iteration=context.iteration,
                )
                # Spec §4.4 — emit an End-output event so the session
                # translator can append an ``assistant_token`` record
                # to messages.jsonl carrying the graph's final output.
                # Storage-backed taps that don't care just drop it.
                await queue.put(
                    _GraphEndOutputEvent(  # type: ignore[arg-type]
                        text=res.text,
                        parsed=res.parsed,
                        end_node_id=node_id,
                    )
                )
                await queue.put(
                    _NodeDone(node_id=node_id, output=out, error=None)
                )
                return
            if isinstance(node, _ToolCallNode):
                # Spec B §2.3 — ToolCall fires the configured tool via the
                # executor's _dispatch_toolcall hook (workspace_executor
                # wires the workspace session's ToolExecutionManager; tests
                # inject a stub). Phase 3 covers the happy path + failure
                # mapping; Phase 6 wires the approval-yielding path
                # (`_GraphToolCallYield`).
                try:
                    args = _resolve_toolcall_arguments(node, context)
                except Exception as exc:  # noqa: BLE001 — Jinja / JSON parse
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=str(exc),
                            ended_detail="template_error",
                        )
                    )
                    return
                try:
                    result = await self._dispatch_toolcall(node, args)
                except YieldToWorker as yld:
                    # Spec B §2.3 step 3 / Phase 6 — the tool engine raised
                    # YieldToWorker because the approval gate fired. Defer
                    # the ToolCall: record a pending entry and post a
                    # suspended sentinel so the outer loop knows to leave
                    # this node's status unchanged. The executor saves a
                    # checkpoint after the superstep settles and re-raises
                    # YieldToWorker upward; the worker catches it, parks
                    # the session, and resumes via
                    # :meth:`_BaseGraphExecutor.resume_from_checkpoint`
                    # once the operator approves.
                    self._pending_toolcalls.append(
                        _PendingToolCall(
                            node_id=node_id,
                            tool_call_id=yld.tool_call_id,
                            parked_event_key=yld.yielded.event_key,
                            arguments=args,
                        )
                    )
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=None,
                            ended_detail=None,
                            suspended=True,
                        )
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=str(exc),
                            ended_detail="tool_execution_failed",
                        )
                    )
                    return
                mapped = _map_toolcall_result(
                    result, output_schema=node.output_schema
                )
                if mapped.error_code is not None:
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=mapped.error_message or mapped.error_code,
                            ended_detail=mapped.error_code,
                        )
                    )
                    return
                tc_out = NodeOutput(
                    text=mapped.text,
                    parsed=mapped.parsed,
                    history=[],
                    iteration=context.iteration,
                )
                await queue.put(
                    _NodeDone(node_id=node_id, output=tc_out, error=None)
                )
                return
            if isinstance(node, _BeginNode):
                # Begin is pure data-shaping; no LLM call, no events emitted.
                # The base executor stores initial_input as a list[Message];
                # the workspace executor (Phase 4) widens that union to
                # also carry dict/str via session metadata.
                gi = context.initial_input
                if isinstance(gi, list):
                    output: NodeOutput | None = _materialise_begin_output(
                        graph_input=None, initial_messages=gi
                    )
                else:
                    output = _materialise_begin_output(
                        graph_input=gi, initial_messages=[]
                    )
            elif isinstance(node, _GraphNodeRef):
                output = await self._stream_subgraph_node(
                    node, context, queue, extra_scope=extra_scope
                )
            elif isinstance(node, _AgentNodeRef):
                output = await self._stream_agent_node(
                    node, context, queue, extra_scope=extra_scope
                )
            else:  # pragma: no cover -- discriminated union exhausted
                raise ConfigError(
                    f"unknown node kind: {type(node).__name__}"
                )
            await queue.put(
                _NodeDone(node_id=node_id, output=output, error=None)
            )
        except YieldToWorker as yld:
            if isinstance(node, _AgentNodeRef):
                # Defer the agent node: record a pending agent-yield and
                # post a suspended sentinel so the superstep leaves it
                # unresolved. The executor checkpoints + re-raises after
                # the superstep settles (mirrors the ToolCall path).
                # Unified nested-yield: when the node's agent turn yielded from
                # INSIDE a nested invoke_agent invocation, ``yld.frames`` carries
                # the in-flight subagent chain (root-first) and ``yld.yielded``
                # is the deeper leaf. Preserve both so the worker can run the
                # continuation walk on resume; an empty stack (the node's own
                # ask_user / approval gate) keeps this park byte-identical.
                from primer.worker.frames import frames_to_jsonable
                nested_frames = list(getattr(yld, "frames", None) or [])
                self._pending_agent_yields.append(
                    _PendingAgentYield(
                        node_id=node_id,
                        tool_call_id=yld.tool_call_id,
                        event_key=yld.yielded.event_key,
                        tool_name=yld.yielded.tool_name,
                        resume_metadata=dict(yld.yielded.resume_metadata or {}),
                        llm_messages=list(yld.llm_messages or []),
                        iteration=context.iteration,
                        frames=frames_to_jsonable(nested_frames) if nested_frames else [],
                        leaf=yld.yielded.to_jsonable() if nested_frames else None,
                    )
                )
                await queue.put(
                    _NodeDone(
                        node_id=node_id, output=None, error=None,
                        ended_detail=None, suspended=True,
                    )
                )
                return
            # Non-agent yield (e.g. from a subgraph node): preserve the
            # prior behaviour of recording it as a node error.
            await queue.put(
                _NodeDone(node_id=node_id, output=None, error=yld)
            )
        except BaseException as exc:
            await queue.put(
                _NodeDone(node_id=node_id, output=None, error=exc)
            )
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _stream_subgraph_node(
        self,
        node: _GraphNodeRef,
        context: GraphContext,
        queue: "asyncio.Queue[StreamEvent | _NodeDone]",
        *,
        extra_scope: dict[str, Any] | None = None,
    ) -> NodeOutput:
        """Recurse into a subgraph; forward events under the parent node id.

        ``extra_scope`` carries per-fan-out-instance vars (``fanout_index``,
        ``fanout_item``) for synthesized invocations (Spec B §2.1).
        """
        if self._graph_resolver is None:
            raise ConfigError(
                f"subgraph node {node.id!r} requires a graph_resolver "
                "to be passed to the executor's constructor"
            )
        sub_graph = await self._graph_resolver(node.graph_id)
        sub_executor = await self._build_sub_executor(node, sub_graph)

        rendered = render_input_template(
            node.input_template, context=context, extra_scope=extra_scope
        )
        sub_input = [Message(role="user", parts=[TextPart(text=rendered)])]

        # Forward every sub-event under THIS node's id so external taps
        # see the parent-graph node's namespace, not the inner one.
        # Track text deltas to assemble a text NodeOutput for downstream
        # consumers. The runtime terminal-event dataclasses
        # (_GraphErrorEvent, _GraphEndOutputEvent) aren't real
        # :class:`StreamEvent`s and don't survive ``_wrap_event``'s
        # ``.type`` access — forward them as-is so the parent's
        # aggregator can pass them on to taps.
        text_buf: list[str] = []
        async for sub_event in sub_executor.invoke(sub_input):
            if isinstance(sub_event, (_GraphErrorEvent, _GraphEndOutputEvent)):
                await queue.put(sub_event)  # type: ignore[arg-type]
                continue
            await queue.put(
                self._wrap_event(sub_event, node.id, context.iteration)
            )
            ev_type = getattr(sub_event, "type", None)
            if ev_type == "text-delta":
                delta = getattr(sub_event, "text", None)
                if delta:
                    text_buf.append(delta)

        return NodeOutput(
            text="".join(text_buf),
            parsed=None,
            history=[],
            iteration=context.iteration,
        )

    # ---- Edge evaluation -------------------------------------------------

    async def _compute_next_ready(
        self,
        just_ran: set[str],
        context: GraphContext,
    ) -> set[str]:
        """Walk outgoing edges from ``just_ran``; return the next ready set.

        Spec B §2.2: for FanIn targets, defer admission until every incoming
        edge's source has produced output (treating fan-out targets as the
        full set of synthesized instances).

        Synthesized fan-out instance ids (e.g. ``"worker[2]"``) don't carry
        outgoing edges of their own — the executor walks the edges of the
        underlying target node id (e.g. ``"worker"``) instead. This keeps
        graph authors free to write the natural ``worker -> fanin`` edge
        once even when ``worker`` is fan-out target with N instances.
        """
        # Build the effective edge-source set: each just-ran id contributes
        # its own outgoing edges; synthesized fan-out instances also
        # contribute their bare target's outgoing edges (de-duplicated).
        edge_sources: set[str] = set(just_ran)
        for nid in just_ran:
            inst = self._fanout_instances.get(nid)
            if inst is not None:
                edge_sources.add(inst.target_node_id)
        # Phase 1: resolve every outgoing edge to its concrete target. We must
        # know the FULL set of nodes scheduled this pass BEFORE gating any
        # FanIn, because a callable router resolved here may schedule a node
        # that itself feeds the FanIn (and the FanIn must then wait for it).
        candidates: list[str] = []
        for nid in edge_sources:
            for edge in self._edges_by_from.get(nid, []):
                if isinstance(edge, _StaticEdge):
                    candidates.append(edge.to_node)
                else:  # _ConditionalEdge
                    target_opt = await self._evaluate_conditional(edge, context)
                    if target_opt is None:
                        continue
                    candidates.append(target_opt)
        # Every resolved target is now "live" (admitted at least once); a
        # callable-router source admitted here is one the FanIn gate must
        # still wait on if it has not yet produced output.
        self._admitted.update(candidates)
        # Phase 2: admit, gating FanIn targets on upstream completion.
        next_ready: set[str] = set()
        for target in candidates:
            target_node = self._nodes_by_id.get(target)
            if isinstance(target_node, _FanInNode):
                if not self._fanin_ready(target_node, context):
                    continue
            next_ready.add(target)
        self._admitted.update(next_ready)
        return next_ready

    def _fanin_ready(
        self, node: "_FanInNode", context: GraphContext
    ) -> bool:
        """Return True iff every incoming edge's source has produced output.

        Spec B §2.2. Three upstream kinds are gated:

        * static / json-path-conditional edges: the source must have a
          ``NodeOutput`` in ``context.nodes`` (``_edges_by_to`` index).
        * fan-out sources: all N synthesized instances must have produced
          output (compare non-``None`` count against the spawning FanOut's
          expected instance count; the aggregator list is positionally
          aligned and may carry ``None`` placeholders).
        * callable-router sources: the router's target is unknown
          statically, so any callable-router source that is *live* (has been
          admitted this run but has not yet produced output) is treated as a
          potential upstream the FanIn must wait for. Once such a source
          produces output its routing decision is settled: if it routed here
          the existing static/json-path or list checks above already account
          for it; if it routed elsewhere it simply never blocks. A
          callable-router source that never activates is never ``_admitted``,
          so it cannot dead-lock the FanIn.
        """
        for edge in self._edges_by_to.get(node.id, []):
            src = getattr(edge, "from_node", None)
            if src is None:
                continue
            entry = context.nodes.get(src)
            if entry is None:
                return False
            if isinstance(entry, list):
                expected = self._fanout_target_expected_count.get(src)
                # The aggregator list is positionally aligned and may carry
                # ``None`` placeholders for instances that have not reported
                # yet. Count only the slots that have actually produced output
                # (``len`` would overcount past-end padding and undercount is
                # impossible since we pad-to-index).
                produced = sum(1 for x in entry if x is not None)
                if expected is None or produced < expected:
                    return False
        # Callable-router upstreams: a source that has been admitted this run
        # but has not yet produced output may still route into this FanIn, so
        # defer admission until it resolves.
        for src in self._callable_router_sources:
            if src in self._admitted and context.nodes.get(src) is None:
                return False
        return True

    async def _evaluate_conditional(
        self,
        edge: _ConditionalEdge,
        context: GraphContext,
    ) -> str | None:
        source_output = context.nodes.get(edge.from_node)
        if source_output is None:
            raise ConfigError(
                f"conditional edge from {edge.from_node!r} fired before "
                "the source node produced output"
            )
        router = edge.router
        if isinstance(router, _JsonPathRouter):
            if source_output.parsed is None:
                raise ConfigError(
                    f"conditional edge from {edge.from_node!r} uses a "
                    "json_path router but the source node has no parsed "
                    "output (response_format is required)"
                )
            match = first_matching_branch(source_output.parsed, router.branches)
            if match is not None:
                target = match.to_node
            elif router.default_to is not None:
                target = router.default_to
            else:
                raise _RoutingFailed(
                    edge.from_node,
                    f"json_path router on edge from {edge.from_node!r} "
                    "matched no branch and has no default_to",
                )
        elif isinstance(router, _CallableRouter):
            target = await self._router_registry.resolve(
                router.callable_id,
                context=context,
                source=source_output,
            )
        else:  # pragma: no cover -- discriminated union exhausted above.
            raise ConfigError(f"unknown router kind: {type(router).__name__}")

        if target not in self._nodes_by_id:
            raise ConfigError(
                f"router returned target {target!r} that is not a known node id"
            )
        return target

    # ---- Stream-event wrapping -------------------------------------------

    @staticmethod
    def _wrap_event(
        event: StreamEvent,
        node_id: str,
        iteration: int,
    ) -> StreamEvent:
        return ExtendedEvent(
            extended=_GraphNodeEvent(
                node_id=node_id,
                iteration=iteration,
                inner_type=event.type,
                inner_payload=event.model_dump(mode="json"),
            )
        )


__all__ = ["_BaseGraphExecutor"]
