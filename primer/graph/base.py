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
from primer.model.workspace_session import SessionStatus
from primer.model.yield_ import Yielded, YieldToWorker


if TYPE_CHECKING:
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.chat import ToolResultPart
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


@dataclass(frozen=True)
class _FanInOutputResult:
    """Outcome of rendering a :class:`_FanInNode`'s aggregate_template.

    Spec B §2.2 — mirrors :class:`_EndOutputResult` so the executor can use
    the same error-code surface (``template_error`` / ``end_output_invalid``)
    for both End-node and FanIn-node failures.
    """

    text: str
    parsed: dict[str, Any] | None
    error_code: str | None
    error_message: str | None = None


def _render_fanin_output(
    fanin: "_FanInNode", context: "GraphContext"
) -> _FanInOutputResult:
    """Render FanIn.aggregate_template + validate optional output_schema.

    Mirrors :func:`_render_end_output` (Spec A §5.4) — failures map to:

    * Jinja error during render → ``template_error``
    * JSON parse failure when output_schema is set → ``end_output_invalid``
    * :class:`jsonschema.ValidationError` → ``end_output_invalid``
    """
    from primer.graph.template import render_template_safely

    if not fanin.aggregate_template:
        return _FanInOutputResult(text="", parsed=None, error_code=None)

    try:
        text = render_template_safely(fanin.aggregate_template, context)
    except Exception as exc:  # noqa: BLE001 — UndefinedError, TemplateSyntaxError, ...
        return _FanInOutputResult(
            text="",
            parsed=None,
            error_code="template_error",
            error_message=str(exc),
        )

    if fanin.output_schema is None:
        return _FanInOutputResult(text=text, parsed=None, error_code=None)

    try:
        parsed_obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return _FanInOutputResult(
            text=text,
            parsed=None,
            error_code="end_output_invalid",
            error_message=f"output is not JSON: {exc}",
        )

    import jsonschema  # local import keeps base.py import cheap

    try:
        jsonschema.validate(instance=parsed_obj, schema=fanin.output_schema)
    except jsonschema.ValidationError as exc:
        return _FanInOutputResult(
            text=text,
            parsed=None,
            error_code="end_output_invalid",
            error_message=exc.message,
        )

    if not isinstance(parsed_obj, dict):
        return _FanInOutputResult(text=text, parsed=None, error_code=None)
    return _FanInOutputResult(text=text, parsed=parsed_obj, error_code=None)


@dataclass(frozen=True)
class _ToolCallOutputResult:
    """Outcome of mapping a :class:`ToolResultPart` into a :class:`NodeOutput`.

    Spec B §2.3 step 4 — mirrors :class:`_EndOutputResult` /
    :class:`_FanInOutputResult` so the executor can use the same error-code
    surface (``tool_output_invalid``) for ToolCall-node output validation.
    """

    text: str
    parsed: dict[str, Any] | None
    error_code: str | None
    error_message: str | None = None


def _map_toolcall_result(
    result: "ToolResultPart",
    *,
    output_schema: dict[str, Any] | None,
) -> _ToolCallOutputResult:
    """Map a :class:`ToolResultPart` into a NodeOutput-ish result.

    Spec B §2.3 step 4:

    * ``text = result.output`` always.
    * When ``output_schema`` is set, parse ``text`` as JSON and validate
      against the schema; on parse / validation failure, return
      ``error_code='tool_output_invalid'``.
    * When the parsed JSON is not a dict, ``parsed`` stays ``None`` because
      :class:`NodeOutput.parsed` is dict-typed; validation against non-object
      schemas still succeeds (no ``error_code``).
    """
    text = result.output
    if output_schema is None:
        return _ToolCallOutputResult(text=text, parsed=None, error_code=None)

    try:
        parsed_obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return _ToolCallOutputResult(
            text=text,
            parsed=None,
            error_code="tool_output_invalid",
            error_message=f"output is not JSON: {exc}",
        )

    import jsonschema  # local import keeps base.py import cheap

    try:
        jsonschema.validate(instance=parsed_obj, schema=output_schema)
    except jsonschema.ValidationError as exc:
        return _ToolCallOutputResult(
            text=text,
            parsed=None,
            error_code="tool_output_invalid",
            error_message=exc.message,
        )

    if not isinstance(parsed_obj, dict):
        # Schema validates non-objects too; NodeOutput.parsed is dict-only.
        return _ToolCallOutputResult(text=text, parsed=None, error_code=None)
    return _ToolCallOutputResult(text=text, parsed=parsed_obj, error_code=None)


def _resolve_toolcall_arguments(
    node: "_ToolCallNode",
    context: "GraphContext",
) -> dict[str, Any]:
    """Resolve a ToolCallNode's arguments against the GraphContext.

    Spec B §2.3 step 1:

    * When ``arguments_template`` is set: render it as Jinja, parse as JSON,
      return the dict. JSON parse failure raises :class:`ValueError` (caller
      maps it to ``ended_detail='template_error'``).
    * Otherwise: walk ``arguments`` recursively. Any string leaf is rendered
      as a Jinja template against GraphContext; non-string leaves pass through
      unchanged.
    """
    from primer.graph.template import render_template_safely

    if node.arguments_template:
        text = render_template_safely(node.arguments_template, context)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"arguments_template did not render to valid JSON: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise ValueError(
                "arguments_template must render to a JSON object"
            )
        return result

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return render_template_safely(value, context)
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return {k: _walk(v) for k, v in node.arguments.items()}


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


class _GraphToolCallYield(Exception):
    """Raised by ``_dispatch_toolcall`` to signal mid-graph approval yield.

    Spec B §2.3 step 3 — Phase 6 wires the executor to checkpoint state
    and re-raise this up through dispatch so the session transitions to
    ``WAITING``. Phase 3 catches it and fails the node so users get a
    clear "approval-yielding not yet enabled" signal during the interim
    rather than a silent hang.
    """


class _ToolApprovalRejected(Exception):
    """Raised on the resume path when the operator rejected the approval.

    Spec B §4.8 / Phase 6 Task 6.4 — the synthetic exception the resume
    drain catches and translates into a node-level
    ``ended_detail='tool_execution_failed'``. The worker's resume hook
    (or a test stub for the storage-backed executor) raises this when
    it sees a ``rejected`` / ``cancelled`` / ``timeout`` decision on the
    parked-state event, instead of re-dispatching the original tool.
    """

    def __init__(self, reason: str | None = None, *, tool_call_id: str | None = None) -> None:
        super().__init__(reason or "tool approval rejected")
        self.reason = reason
        self.tool_call_id = tool_call_id


class _RoutingFailed(Exception):
    """Raised when a conditional edge matches no branch and has no default.

    Carries the source node id so the executor's outer loop can emit a
    :class:`_GraphErrorEvent` with ``code='routing_failed'`` and the
    right ``node_id`` payload (spec §5.4).
    """

    def __init__(self, source_node_id: str, message: str) -> None:
        super().__init__(message)
        self.source_node_id = source_node_id


class _FanoutSourceInvalid(Exception):
    """Raised when a FanOutSpec(kind='map') source path doesn't resolve to a list.

    Caught by the executor's outer loop and translated to
    ``ended_detail="fanout_source_invalid"`` per Spec B §1.4.
    """

    def __init__(self, source_node_id: str, source_path: str, reason: str) -> None:
        self.source_node_id = source_node_id
        self.source_path = source_path
        self.reason = reason
        super().__init__(
            f"FanOut map source {source_node_id!r}.{source_path!r}: {reason}"
        )


@dataclass(frozen=True)
class _FanoutInstance:
    """One synthesized instance produced by a FanOutSpec.

    The executor dispatches one instance per row, recording each
    completed NodeOutput at ``GraphContext.nodes[synthesized_id]`` and
    accumulating the aggregator list at ``GraphContext.nodes[target_node_id]``.
    """

    synthesized_id: str            # e.g. "worker[2]" (broadcast/map) or "b" (tee)
    target_node_id: str            # the underlying node definition
    fanout_index: int | None       # None for tee
    fanout_item: Any               # FanOut's NodeOutput for broadcast/tee; list element for map


@dataclass
class _FanoutDrainState:
    """Per-(FanOut, target) drain bookkeeping for non-fail_fast modes.

    Spec B §1.4 / §2.5:

    * ``drain_then_fail`` — once every instance for ``target_node_id`` has
      reported (success or failure), terminate the graph ``failed`` with
      ``ended_detail='fanin_upstream_failed'``.
    * ``collect`` — stamp failed instances' ``NodeOutput.error`` /
      ``ended_detail`` and let the graph continue; downstream FanIn templates
      branch on ``n.error``.
    """

    on_failure: str   # "fail_fast" | "drain_then_fail" | "collect"
    fanout_node_id: str
    target_node_id: str
    expected_count: int
    completed_count: int = 0
    any_failed: bool = False
    first_failure: tuple[str, str] | None = None  # (synthesized_id, ended_detail)


def _resolve_fanout_spec(
    spec: "FanOutSpec",
    context: "GraphContext",
    fanout_output: "NodeOutput",
) -> list[_FanoutInstance]:
    """Walk one FanOutSpec into the list of instances to dispatch.

    Spec B §2.1:
    - broadcast → N instances of ``target_node_id`` named ``target[i]``.
    - tee → one instance per id in ``target_node_ids`` (no synthesized
      index — instance id == target id).
    - map → one instance per element of the source list at
      ``source_node_id.parsed.<source_path>``; raises
      :class:`_FanoutSourceInvalid` when the path doesn't resolve or
      doesn't land on a list.
    """
    from primer.graph.router import _resolve_path

    if spec.kind == "broadcast":
        target = spec.target_node_id or ""
        n = spec.count or 0
        return [
            _FanoutInstance(
                synthesized_id=f"{target}[{i}]",
                target_node_id=target,
                fanout_index=i,
                fanout_item=fanout_output,
            )
            for i in range(n)
        ]
    if spec.kind == "tee":
        return [
            _FanoutInstance(
                synthesized_id=tid,
                target_node_id=tid,
                fanout_index=None,
                fanout_item=fanout_output,
            )
            for tid in (spec.target_node_ids or [])
        ]
    # map
    source_node_id = spec.source_node_id or ""
    source_path = spec.source_path or ""
    source_node = context.nodes.get(source_node_id)
    if source_node is None or isinstance(source_node, list):
        raise _FanoutSourceInvalid(
            source_node_id, source_path,
            "source node has no parsed output (or is a fan-out target)",
        )
    parsed = source_node.parsed
    if parsed is None:
        raise _FanoutSourceInvalid(
            source_node_id, source_path,
            "source node has no parsed output",
        )
    found, value = _resolve_path(parsed, source_path)
    if not found:
        raise _FanoutSourceInvalid(
            source_node_id, source_path,
            "path did not resolve",
        )
    if not isinstance(value, list):
        raise _FanoutSourceInvalid(
            source_node_id, source_path,
            f"resolved to non-list value (type={type(value).__name__})",
        )
    target = spec.target_node_id or ""
    return [
        _FanoutInstance(
            synthesized_id=f"{target}[{i}]",
            target_node_id=target,
            fanout_index=i,
            fanout_item=item,
        )
        for i, item in enumerate(value)
    ]


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

    __slots__ = ("node_id", "output", "error", "ended_detail", "suspended")

    def __init__(
        self,
        *,
        node_id: str,
        output: NodeOutput | None,
        error: BaseException | str | None,
        ended_detail: str | None = None,
        suspended: bool = False,
    ) -> None:
        self.node_id = node_id
        self.output = output
        self.error = error
        self.ended_detail = ended_detail
        # Spec B §2.3 step 3 / Phase 6 — when ``True``, the node yielded
        # for approval and is suspended pending operator decision. The
        # outer loop must NOT mark it ENDED/FAILED, nor record output;
        # the executor tracks it via ``_pending_toolcalls`` and resumes
        # via :meth:`_BaseGraphExecutor.resume_from_checkpoint`.
        self.suspended = suspended


@dataclass(frozen=True)
class _PendingToolCall:
    """One ToolCall node suspended on an approval yield.

    Spec B §2.3 step 3 / Phase 6. Captured at the moment the executor
    sees :class:`YieldToWorker` bubble up from ``_dispatch_toolcall``;
    persisted into the checkpoint payload so a resumed executor can
    re-dispatch the same call with ``bypass_approval=True``.

    Attributes
    ----------
    node_id
        The graph node id (or synthesized fan-out instance id) that
        suspended. Used by :meth:`resume_from_checkpoint` to look up
        the underlying ``_ToolCallNode`` definition.
    tool_call_id
        Stable id of the parked tool invocation. Mirrors the same
        field on agent-side yielding tools so the worker's parked
        state shape is identical.
    parked_event_key
        Routing key the operator publishes on to wake the park —
        ``tool_approval:<session_id>:<tool_call_id>`` by convention.
    arguments
        The arguments dict resolved at original-dispatch time.
        Replaying with the same dict + ``bypass_approval=True`` keeps
        the resumed call semantically identical to a freshly-approved
        first dispatch.
    """

    node_id: str
    tool_call_id: str
    parked_event_key: str
    arguments: dict[str, Any]


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
        self._edges_by_to: dict[str, list] = {}
        for e in graph.edges:
            self._edges_by_from.setdefault(e.from_node, []).append(e)
            # Only static + json-path conditional edges have statically-known
            # ``to_node``s; conditional + callable router targets are skipped
            # (FanIn ready-set treats them as already-satisfied since their
            # source completion already records output to context.nodes).
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
        # ``_context`` and ``_ready_set`` are populated by :meth:`invoke`
        # at the top of each superstep and kept on the executor so
        # :meth:`snapshot_state` can serialise them mid-flight. ``None``
        # before the first superstep / after termination.
        self._context: GraphContext | None = None
        self._ready_set: set[str] = set()
        self._node_states: dict[str, NodeRuntimeState] = {}

    @property
    def graph(self) -> Graph:
        return self._graph

    # ---- Checkpoint payload (Phase 6 / Spec B §2.3 step 3) --------------

    def snapshot_state(self) -> dict[str, Any]:
        """Serialise the executor's mid-flight state into a JSON-compatible dict.

        Used by :meth:`invoke` when a ToolCall node yields for approval —
        the worker persists this payload onto the session's parked state,
        and a fresh executor calls :meth:`restore_state` on the resume
        path to reconstruct the world before draining the pending
        ToolCalls.

        Fields:

        * ``context`` — :class:`GraphContext` via ``model_dump(mode="json")``.
        * ``ready_set`` — the sorted list of node ids the outer loop was
          about to run when the yield fired (so resume can re-enter the
          superstep loop at the same point).
        * ``node_states`` — per-node :class:`NodeRuntimeState`, json-dumped.
        * ``fanout_instances`` — synthesized_id → instance dict.
        * ``fanout_target_expected_count`` — target_id → expected count.
        * ``instance_to_spec`` — synthesized_id →
          ``{"fanout_node_id": ..., "spec": <FanOutSpec.model_dump>}``.
        * ``fanout_drain_state`` — drain_key → drain_state dict.
        * ``pending_toolcalls`` — list of pending ToolCall dicts.
        """
        ctx_payload: dict[str, Any] | None = None
        if self._context is not None:
            ctx_payload = self._context.model_dump(mode="json")
        return {
            "context": ctx_payload,
            "ready_set": sorted(self._ready_set),
            "node_states": {
                nid: ns.model_dump(mode="json")
                for nid, ns in self._node_states.items()
            },
            "fanout_instances": {
                sid: {
                    "synthesized_id": inst.synthesized_id,
                    "target_node_id": inst.target_node_id,
                    "fanout_index": inst.fanout_index,
                    "fanout_item": (
                        inst.fanout_item.model_dump(mode="json")
                        if isinstance(inst.fanout_item, NodeOutput)
                        else inst.fanout_item
                    ),
                    "fanout_item_kind": (
                        "node_output"
                        if isinstance(inst.fanout_item, NodeOutput)
                        else "raw"
                    ),
                }
                for sid, inst in self._fanout_instances.items()
            },
            "fanout_target_expected_count": dict(
                self._fanout_target_expected_count
            ),
            "instance_to_spec": {
                sid: {
                    "fanout_node_id": fanout_id,
                    "spec": spec.model_dump(mode="json"),
                }
                for sid, (fanout_id, spec) in self._instance_to_spec.items()
            },
            "fanout_drain_state": {
                key: {
                    "on_failure": ds.on_failure,
                    "fanout_node_id": ds.fanout_node_id,
                    "target_node_id": ds.target_node_id,
                    "expected_count": ds.expected_count,
                    "completed_count": ds.completed_count,
                    "any_failed": ds.any_failed,
                    "first_failure": list(ds.first_failure) if ds.first_failure else None,
                }
                for key, ds in self._fanout_drain_state.items()
            },
            "pending_toolcalls": [
                {
                    "node_id": p.node_id,
                    "tool_call_id": p.tool_call_id,
                    "parked_event_key": p.parked_event_key,
                    "arguments": dict(p.arguments),
                }
                for p in self._pending_toolcalls
            ],
        }

    def restore_state(self, payload: dict[str, Any]) -> None:
        """Inverse of :meth:`snapshot_state` — repopulate executor attrs.

        The graph topology + resolvers stay as-passed at construction
        time; only the dynamic execution state is reconstructed. Callers
        that mutated the topology between checkpoint + resume are on
        their own (Spec B does not yet support graph hot-edits across
        a pause).
        """
        ctx_raw = payload.get("context")
        if ctx_raw is None:
            self._context = None
        else:
            self._context = GraphContext.model_validate(ctx_raw)
        self._ready_set = set(payload.get("ready_set") or [])
        self._node_states = {
            nid: NodeRuntimeState.model_validate(raw)
            for nid, raw in (payload.get("node_states") or {}).items()
        }
        self._fanout_instances = {}
        for sid, raw in (payload.get("fanout_instances") or {}).items():
            kind = raw.get("fanout_item_kind", "raw")
            item_raw = raw.get("fanout_item")
            if kind == "node_output" and item_raw is not None:
                item: Any = NodeOutput.model_validate(item_raw)
            else:
                item = item_raw
            self._fanout_instances[sid] = _FanoutInstance(
                synthesized_id=raw["synthesized_id"],
                target_node_id=raw["target_node_id"],
                fanout_index=raw.get("fanout_index"),
                fanout_item=item,
            )
        self._fanout_target_expected_count = dict(
            payload.get("fanout_target_expected_count") or {}
        )
        self._instance_to_spec = {}
        for sid, raw in (payload.get("instance_to_spec") or {}).items():
            spec = FanOutSpec.model_validate(raw["spec"])
            self._instance_to_spec[sid] = (raw["fanout_node_id"], spec)
        self._fanout_drain_state = {}
        for key, raw in (payload.get("fanout_drain_state") or {}).items():
            ff = raw.get("first_failure")
            first_failure = tuple(ff) if ff else None
            self._fanout_drain_state[key] = _FanoutDrainState(
                on_failure=raw["on_failure"],
                fanout_node_id=raw["fanout_node_id"],
                target_node_id=raw["target_node_id"],
                expected_count=raw["expected_count"],
                completed_count=raw.get("completed_count", 0),
                any_failed=raw.get("any_failed", False),
                first_failure=first_failure,  # type: ignore[arg-type]
            )
        self._pending_toolcalls = [
            _PendingToolCall(
                node_id=raw["node_id"],
                tool_call_id=raw["tool_call_id"],
                parked_event_key=raw["parked_event_key"],
                arguments=dict(raw.get("arguments") or {}),
            )
            for raw in (payload.get("pending_toolcalls") or [])
        ]

    async def resume_from_checkpoint(
        self,
        checkpoint: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        """Restore from a checkpoint and continue graph execution.

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

        # Drain pending ToolCalls. We snapshot the list so re-yields during
        # drain (should not happen with bypass_approval=True) accumulate
        # into a fresh _pending_toolcalls that the post-drain check below
        # can re-yield to the worker.
        pending = list(self._pending_toolcalls)
        self._pending_toolcalls = []
        completed_ids: list[str] = []
        for entry in pending:
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
                                    target_len = (inst_obj.fanout_index or 0) + 1
                                    while len(agg_list) < target_len:
                                        agg_list.append(None)
                                    if inst_obj.fanout_index is not None:
                                        agg_list[inst_obj.fanout_index] = fail_output
                                    else:
                                        agg_list.append(fail_output)
                                    context.nodes[inst_obj.target_node_id] = [
                                        x for x in agg_list if x is not None
                                    ]
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
                        target_len = (inst.fanout_index or 0) + 1
                        while len(agg_list) < target_len:
                            agg_list.append(None)
                        if inst.fanout_index is not None:
                            agg_list[inst.fanout_index] = done.output
                        else:
                            agg_list.append(done.output)
                        context.nodes[inst.target_node_id] = [
                            x for x in agg_list if x is not None
                        ]
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
            if self._pending_toolcalls:
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
                first = self._pending_toolcalls[0]
                # Stamp the snapshot on the exception so the worker can
                # persist it onto the session's parked-state blob without
                # needing a separate ``snapshot_state`` round-trip.
                yld = YieldToWorker(
                    Yielded(
                        tool_name="_approval",
                        event_key=first.parked_event_key,
                    ),
                    tool_call_id=first.tool_call_id,
                )
                # Attach the snapshot payload + the full pending list so
                # the worker / resume path has everything it needs.
                yld.graph_checkpoint = self.snapshot_state()  # type: ignore[attr-defined]
                raise yld

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
        *,
        extra_scope: dict[str, Any] | None = None,
    ) -> NodeOutput:
        """Run one agent-backed node; identical semantics to a standalone agent.

        ``extra_scope`` carries per-fan-out-instance vars (``fanout_index``,
        ``fanout_item``) for synthesized invocations (Spec B §2.1).
        """
        agent = await self._agent_resolver(node.agent_id)
        llm, llm_model = await self._llm_resolver(agent)
        if self._tool_manager_resolver is not None:
            tool_manager = await self._tool_manager_resolver(agent)
        else:
            tool_manager = ToolExecutionManager()

        # Render the input template -> single user-role Message.
        rendered = render_input_template(
            node.input_template, context=context, extra_scope=extra_scope
        )
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
        next_ready: set[str] = set()
        # Build the effective edge-source set: each just-ran id contributes
        # its own outgoing edges; synthesized fan-out instances also
        # contribute their bare target's outgoing edges (de-duplicated).
        edge_sources: set[str] = set(just_ran)
        for nid in just_ran:
            inst = self._fanout_instances.get(nid)
            if inst is not None:
                edge_sources.add(inst.target_node_id)
        for nid in edge_sources:
            for edge in self._edges_by_from.get(nid, []):
                if isinstance(edge, _StaticEdge):
                    target = edge.to_node
                else:  # _ConditionalEdge
                    target_opt = await self._evaluate_conditional(edge, context)
                    if target_opt is None:
                        continue
                    target = target_opt
                # FanIn-specific gate: don't admit until every upstream
                # source has produced output.
                target_node = self._nodes_by_id.get(target)
                if isinstance(target_node, _FanInNode):
                    if not self._fanin_ready(target_node, context):
                        continue
                next_ready.add(target)
        return next_ready

    def _fanin_ready(
        self, node: "_FanInNode", context: GraphContext
    ) -> bool:
        """Return True iff every statically-known incoming edge source has
        produced output. Spec B §2.2.

        Fan-out sources count as "all N synthesized instances must have
        produced output" — we compare ``len(context.nodes[src])`` against
        the spawning FanOut's expected instance count.
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
                if expected is None or len(entry) < expected:
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
