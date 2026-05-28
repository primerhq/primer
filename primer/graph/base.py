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
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
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
    _GraphNodeEvent,
)
from primer.model.except_ import ConfigError
from primer.model.graph import (
    Graph,
    GraphContext,
    NodeOutput,
    NodeRuntimeState,
    NodeRuntimeStatus,
    _AgentNodeRef,
    _CallableRouter,
    _ConditionalEdge,
    _GraphNodeRef,
    _JsonPathRouter,
    _StaticEdge,
    _TerminalNode,
)
from primer.model.workspace_session import SessionStatus


if TYPE_CHECKING:
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.provider import LLMModel


logger = logging.getLogger(__name__)


class _NodeDone:
    """Sentinel posted to the merge queue when a node finishes streaming."""

    __slots__ = ("node_id", "output", "error")

    def __init__(
        self,
        *,
        node_id: str,
        output: NodeOutput | None,
        error: BaseException | None,
    ) -> None:
        self.node_id = node_id
        self.output = output
        self.error = error


class _BaseGraphExecutor(ABC):
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
        for e in graph.edges:
            self._edges_by_from.setdefault(e.from_node, []).append(e)

    @property
    def graph(self) -> Graph:
        return self._graph

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

    # ---- Public surface --------------------------------------------------

    async def invoke(
        self,
        messages: list[Message],
    ) -> AsyncIterator[StreamEvent]:
        """Execute the graph; stream events live as they happen.

        Concurrent nodes within a superstep stream their events
        through a shared :class:`asyncio.Queue`; the caller sees
        events in arrival order, interleaved across nodes. Each
        event is wrapped in
        :class:`ExtendedEvent(_GraphNodeEvent(...))` carrying the
        ``node_id`` and ``iteration`` so consumers can demultiplex.
        """
        node_states: dict[str, NodeRuntimeState] = {
            n.id: NodeRuntimeState(status=NodeRuntimeStatus.PENDING)
            for n in self._graph.nodes
        }
        context = GraphContext(
            initial_input=list(messages),
            iteration=0,
            nodes={},
        )
        ready: set[str] = {self._graph.entry_node_id}
        ended_reason: str | None = None

        while ready:
            # Cycle bound check.
            if (
                self._graph.max_iterations is not None
                and context.iteration >= self._graph.max_iterations
            ):
                ended_reason = "max_iterations_exceeded"
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

            # Run all ready nodes concurrently. Each pushes its events
            # live to the shared queue; we drain them as they arrive
            # and yield to the caller. _NodeDone sentinels track
            # completion + per-node final result.
            ready_ordered = list(ready)
            queue: "asyncio.Queue[StreamEvent | _NodeDone]" = asyncio.Queue()
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
                    except (asyncio.CancelledError, BaseException):
                        pass

            # Apply per-node results to the supersteps' node_states +
            # graph context, decide whether to terminate.
            any_failed = False
            terminal_reached = False
            for nid in ready_ordered:
                done = results.get(nid)
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
                    any_failed = True
                    continue
                node = self._nodes_by_id[nid]
                if isinstance(node, _TerminalNode):
                    terminal_reached = True
                if done.output is not None:
                    context.nodes[nid] = done.output
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

            if any_failed:
                ended_reason = "failed"
                break
            if terminal_reached:
                ended_reason = "completed"
                break

            # Compute next ready set by evaluating outgoing edges.
            try:
                next_ready = await self._compute_next_ready(
                    set(ready_ordered), context
                )
            except ConfigError as exc:
                logger.warning(
                    "GraphExecutor: edge evaluation failed",
                    extra={"graph_id": self._graph.id, "error": str(exc)},
                )
                ended_reason = "failed"
                break

            ready = next_ready
            context.iteration += 1

        if ended_reason is None:
            ended_reason = "completed"

        await self._save_state(
            iteration=context.iteration,
            node_states=node_states,
            status=SessionStatus.ENDED,
            ended_reason=ended_reason,
        )

    # ---- Per-node streaming ----------------------------------------------

    async def _stream_node(
        self,
        node_id: str,
        context: GraphContext,
        queue: "asyncio.Queue[StreamEvent | _NodeDone]",
    ) -> None:
        """Run one node; push events live to ``queue``, then a _NodeDone."""
        node = self._nodes_by_id[node_id]
        try:
            if isinstance(node, _TerminalNode):
                output: NodeOutput | None = NodeOutput(
                    text="", iteration=context.iteration
                )
            elif isinstance(node, _GraphNodeRef):
                output = await self._stream_subgraph_node(
                    node, context, queue
                )
            elif isinstance(node, _AgentNodeRef):
                output = await self._stream_agent_node(node, context, queue)
            else:  # pragma: no cover -- discriminated union exhausted
                raise ConfigError(
                    f"unknown node kind: {type(node).__name__}"
                )
            await queue.put(
                _NodeDone(node_id=node_id, output=output, error=None)
            )
        except BaseException as exc:
            await queue.put(
                _NodeDone(node_id=node_id, output=None, error=exc)
            )
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _stream_agent_node(
        self,
        node: _AgentNodeRef,
        context: GraphContext,
        queue: "asyncio.Queue[StreamEvent | _NodeDone]",
    ) -> NodeOutput:
        """Run one agent-backed node; identical semantics to a standalone agent."""
        agent = await self._agent_resolver(node.agent_id)
        llm, llm_model = await self._llm_resolver(agent)
        if self._tool_manager_resolver is not None:
            tool_manager = await self._tool_manager_resolver(agent)
        else:
            tool_manager = ToolExecutionManager()

        # Render the input template -> single user-role Message.
        rendered = render_input_template(node.input_template, context=context)
        new_user_msg = Message(role="user", parts=[TextPart(text=rendered)])

        # Build the prompt: system + history + new user msg.
        history = await self._load_node_history(node.id)
        prompt: list[Message] = []
        if agent.system_prompt:
            sys_text = "\n\n".join(agent.system_prompt)
            prompt.append(
                Message(role="system", parts=[TextPart(text=sys_text)])
            )
        prompt.extend(history)
        prompt.append(new_user_msg)

        # Delegate to the shared agent loop. Tool dispatch (multi-turn
        # if the LLM emits ToolCallParts) happens transparently here --
        # graph nodes get the same behaviour as standalone agents.
        produced_messages: list[Message] = []
        async for event in run_agent_turn(
            agent=agent,
            llm=llm,
            llm_model=llm_model,
            tool_manager=tool_manager,
            prompt=prompt,
            response_format=node.response_format,
            principal=self._principal,
            messages_out=produced_messages,
        ):
            await queue.put(
                self._wrap_event(event, node.id, context.iteration)
            )

        # Persist the new user msg + every message produced this turn
        # (assistant + any tool result messages from the loop).
        all_new = [new_user_msg] + produced_messages
        await self._persist_node_turn(node.id, context.iteration, all_new)

        # Build NodeOutput from the LAST assistant message (after any
        # tool round-trips).
        last_assistant: Message | None = None
        for msg in reversed(produced_messages):
            if msg.role == "assistant":
                last_assistant = msg
                break
        text = ""
        if last_assistant is not None:
            text = "".join(
                p.text  # type: ignore[union-attr]
                for p in last_assistant.parts
                if p.type == "text"
            )
        parsed: dict[str, Any] | None = None
        if node.response_format is not None and text:
            try:
                loaded = json.loads(text)
                parsed = loaded if isinstance(loaded, dict) else {"value": loaded}
            except json.JSONDecodeError:
                parsed = None

        return NodeOutput(
            text=text,
            parsed=parsed,
            history=history + all_new,
            iteration=context.iteration,
        )

    async def _stream_subgraph_node(
        self,
        node: _GraphNodeRef,
        context: GraphContext,
        queue: "asyncio.Queue[StreamEvent | _NodeDone]",
    ) -> NodeOutput:
        """Recurse into a subgraph; forward events under the parent node id."""
        if self._graph_resolver is None:
            raise ConfigError(
                f"subgraph node {node.id!r} requires a graph_resolver "
                "to be passed to the executor's constructor"
            )
        sub_graph = await self._graph_resolver(node.graph_id)
        sub_executor = await self._build_sub_executor(node, sub_graph)

        rendered = render_input_template(node.input_template, context=context)
        sub_input = [Message(role="user", parts=[TextPart(text=rendered)])]

        # Forward every sub-event under THIS node's id so external taps
        # see the parent-graph node's namespace, not the inner one.
        # Track text deltas to assemble a text NodeOutput for downstream
        # consumers.
        text_buf: list[str] = []
        async for sub_event in sub_executor.invoke(sub_input):
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
        """Walk outgoing edges from ``just_ran``; return the next ready set."""
        next_ready: set[str] = set()
        for nid in just_ran:
            for edge in self._edges_by_from.get(nid, []):
                if isinstance(edge, _StaticEdge):
                    next_ready.add(edge.to_node)
                else:  # _ConditionalEdge
                    target = await self._evaluate_conditional(edge, context)
                    if target is not None:
                        next_ready.add(target)
        return next_ready

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
                raise ConfigError(
                    f"json_path router on edge from {edge.from_node!r} "
                    "matched no branch and has no default_to"
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
