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
    _BeginNode,
    _CallableRouter,
    _ConditionalEdge,
    _EndNode,
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


@dataclass(frozen=True)
class _EndOutputResult:
    """Outcome of rendering an :class:`_EndNode`'s output_template.

    ``error_code`` is one of ``{None, "template_error", "end_output_invalid"}``
    matching spec §5.4. On success ``parsed`` is the parsed JSON object
    (dict only — non-dict schemas are accepted but ``parsed`` stays None
    because :class:`NodeOutput.parsed` is dict-typed).
    """

    text: str
    parsed: dict[str, Any] | None
    error_code: str | None
    error_message: str | None = None


def _render_end_output(end: "_EndNode", context: "GraphContext") -> _EndOutputResult:
    """Render End.output_template; if End.output_schema is set, parse + validate.

    Errors map to spec §5.4 codes:

    * Jinja error during render → ``template_error``
    * JSON parse failure when output_schema is set → ``end_output_invalid``
    * :class:`jsonschema.ValidationError` → ``end_output_invalid``
    """
    from primer.graph.template import render_template_safely

    if not end.output_template:
        return _EndOutputResult(text="", parsed=None, error_code=None)

    try:
        text = render_template_safely(end.output_template, context)
    except Exception as exc:  # noqa: BLE001 -- UndefinedError, TemplateSyntaxError, ...
        return _EndOutputResult(
            text="",
            parsed=None,
            error_code="template_error",
            error_message=str(exc),
        )

    if end.output_schema is None:
        return _EndOutputResult(text=text, parsed=None, error_code=None)

    try:
        parsed_obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return _EndOutputResult(
            text=text,
            parsed=None,
            error_code="end_output_invalid",
            error_message=f"output is not JSON: {exc}",
        )

    import jsonschema  # local import keeps base.py import cheap

    try:
        jsonschema.validate(instance=parsed_obj, schema=end.output_schema)
    except jsonschema.ValidationError as exc:
        return _EndOutputResult(
            text=text,
            parsed=None,
            error_code="end_output_invalid",
            error_message=exc.message,
        )

    if not isinstance(parsed_obj, dict):
        # Schema validates non-objects too; NodeOutput.parsed is dict-only.
        return _EndOutputResult(text=text, parsed=None, error_code=None)
    return _EndOutputResult(text=text, parsed=parsed_obj, error_code=None)


def _materialise_begin_output(
    graph_input: Any,
    initial_messages: list[Message],
) -> NodeOutput:
    """Build the :class:`NodeOutput` for the Begin node.

    Spec §2.1 — Begin is a pure data-shaping node:

    * dict input → ``text`` = JSON, ``parsed`` = the dict
    * str input → ``text`` = the string, ``parsed`` = ``None``
    * list[Message] / None → ``text`` = concatenated text parts,
      ``parsed`` = ``None``

    ``history`` is the message-rendered version of the input.
    """
    if isinstance(graph_input, dict):
        return NodeOutput(
            text=json.dumps(graph_input, ensure_ascii=False),
            parsed=graph_input,
            history=initial_messages,
            iteration=0,
        )
    if isinstance(graph_input, str):
        return NodeOutput(
            text=graph_input,
            parsed=None,
            history=initial_messages,
            iteration=0,
        )
    # list[Message] or None — concatenate text parts.
    parts: list[str] = []
    for msg in initial_messages:
        for part in getattr(msg, "parts", []):
            t = getattr(part, "text", None)
            if isinstance(t, str):
                parts.append(t)
    return NodeOutput(
        text="\n".join(parts),
        parsed=None,
        history=initial_messages,
        iteration=0,
    )


def _resolve_initial_ready_node(graph: "Graph") -> str:
    """Return the id of the unique :class:`_BeginNode` that seeds the
    executor's initial ready set.

    Spec §2.3: the topology validator already guarantees exactly one
    Begin node; this guard is defence in depth against bypassed
    validators (e.g. ``Graph.model_construct``).
    """
    begins = [n for n in graph.nodes if isinstance(n, _BeginNode)]
    if len(begins) != 1:
        raise ValueError(
            f"graph {graph.id!r} must have exactly one Begin node; got {len(begins)}"
        )
    return begins[0].id


class _RoutingFailed(Exception):
    """Raised when a conditional edge matches no branch and has no default.

    Carries the source node id so the executor's outer loop can emit a
    :class:`_GraphErrorEvent` with ``code='routing_failed'`` and the
    right ``node_id`` payload (spec §5.4).
    """

    def __init__(self, source_node_id: str, message: str) -> None:
        super().__init__(message)
        self.source_node_id = source_node_id


@dataclass(frozen=True)
class _GraphErrorEvent:
    """Terminal error event yielded immediately before the graph ends ``failed``.

    Spec §5.4. The workspace executor translates this into a
    ``SessionMessageRecord(kind=error, payload=...)`` on the session
    log; the storage-backed executor leaves it on the stream for taps
    to consume.
    """

    code: str
    message: str
    node_id: str | None
    path: str | None = None


@dataclass(frozen=True)
class _GraphEndOutputEvent:
    """End-node output event yielded immediately after End fires successfully.

    Spec §4.4 / §2.2. Carries the rendered ``text``, optional ``parsed``
    JSON object, and the End node's id. The session-layer translator
    converts this into a ``SessionMessageRecord(kind=assistant_token,
    payload={text, parsed, end_node_id})`` so the session detail page's
    WS stream surfaces the graph's final output the same way an agent's
    final assistant turn does. The terminal ``done`` record continues to
    come from the session-dispatch post-turn path.
    """

    text: str
    parsed: dict[str, Any] | None
    end_node_id: str


class _NodeDone:
    """Sentinel posted to the merge queue when a node finishes streaming."""

    __slots__ = ("node_id", "output", "error", "ended_detail")

    def __init__(
        self,
        *,
        node_id: str,
        output: NodeOutput | None,
        error: BaseException | str | None,
        ended_detail: str | None = None,
    ) -> None:
        self.node_id = node_id
        self.output = output
        self.error = error
        self.ended_detail = ended_detail


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
        ended_reason: str | None = None
        ended_detail: str | None = None

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
            # Error events to yield AFTER the per-node loop (yielding
            # inside the loop while we mutate node_states would be fine,
            # but emitting once we've classified everything keeps the
            # ordering predictable: results first, then the terminal error).
            error_events: list[_GraphErrorEvent] = []
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
                node = self._nodes_by_id[nid]
                if isinstance(node, (_TerminalNode, _EndNode)):
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
                # ended_reason / ended_detail may already be set by the
                # per-node handler above (e.g. End-node failure carries a
                # spec §5.4 code); only fall back when nothing's been set.
                if ended_reason is None:
                    ended_reason = "failed"
                for ev in error_events:
                    yield ev  # type: ignore[misc]
                break
            if terminal_reached:
                ended_reason = "completed"
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
            elif isinstance(node, _TerminalNode):
                output = NodeOutput(
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
