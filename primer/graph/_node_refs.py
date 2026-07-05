"""Module-level value types and pure helpers for the graph executors.

Extracted from :mod:`primer.graph.base` so the executor module holds the
class machinery and this module holds the self-contained pieces: the
frozen result/event dataclasses, the executor control-flow exceptions,
the fan-out instance/drain bookkeeping, the pending-park records, and the
pure render/resolve helpers. Nothing here imports the executor, so it can
be imported freely (``primer.graph.base`` re-exports every public name for
back-compat).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from primer.model.chat import Message
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphContext,
    NodeOutput,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _ToolCallNode,
)


if TYPE_CHECKING:
    from primer.model.chat import ToolResultPart


__all__ = [
    "_EndOutputResult",
    "_render_end_output",
    "_FanInOutputResult",
    "_render_fanin_output",
    "_ToolCallOutputResult",
    "_map_toolcall_result",
    "_resolve_toolcall_arguments",
    "_materialise_begin_output",
    "_resolve_initial_ready_node",
    "_GraphToolCallYield",
    "_ToolApprovalRejected",
    "_RoutingFailed",
    "_FanoutSourceInvalid",
    "_FanoutInstance",
    "_FanoutDrainState",
    "_resolve_fanout_spec",
    "_GraphErrorEvent",
    "_GraphEndOutputEvent",
    "_GraphTransitionEvent",
    "_NodeDone",
    "_PendingToolCall",
    "_PendingAgentYield",
]


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


def _is_value_yield_toolcall(entry: "_PendingToolCall") -> bool:
    """True when a pending tool_call node suspended on a **value-yielding**
    tool (e.g. ``ask_user``) rather than on an approval gate.

    A value-yielding tool's node RESULT is the operator's reply, so on resume
    the executor runs the tool's resume hook on the operator payload instead of
    re-dispatching the call with ``bypass_approval=True`` (the approval-gate
    path). The discriminator is the bare ``tool_name`` the tool stamped onto
    its :class:`~primer.model.yield_.Yielded` at suspend time:

    * ``"_approval"`` (or ``None`` for a legacy park) -> approval gate.
    * anything with a registered resume hook -> value-yield.

    ``None`` (a park written before ``tool_name`` was captured) falls through
    to the approval path so old checkpoints keep their original behaviour.
    """
    name = entry.tool_name
    if not name or name == "_approval":
        return False
    from primer.worker.yield_resume_registry import has_resume_hook

    return has_resume_hook(name)


def _resume_value_yield_toolcall(
    *,
    tool_name: str,
    resume_metadata: dict[str, Any],
    tool_call_id: str,
    payload: "dict[str, Any] | Any",
) -> "ToolResultPart":
    """Run a value-yielding tool's resume hook and shape a ToolResultPart.

    Mirrors :meth:`primer.worker.pool.WorkerPool._graph_agent_tool_result`
    for an agent-node ask_user yield, but for a **tool_call** node: the hook
    turns the operator payload (``{"response": ...}``) / timeout / cancel into
    the tool result the node's downstream consumers read via ``nodes.<id>.text``.
    """
    from primer.model.chat import ToolResultPart
    from primer.worker.yield_resume_registry import get_resume_hook

    hook = get_resume_hook(tool_name)
    hook_result = hook(resume_metadata or {}, payload)
    # The resume hooks in-tree are synchronous; guard against an async hook
    # to keep this helper usable if one is added later.
    if asyncio.iscoroutine(hook_result):  # pragma: no cover -- all hooks sync
        raise RuntimeError(
            f"resume hook for {tool_name!r} is async; tool_call resume only "
            "supports synchronous hooks"
        )
    return ToolResultPart(
        id=tool_call_id,
        output=hook_result.output,
        error=hook_result.is_error,
    )


def _resolve_toolcall_arguments(
    node: "_ToolCallNode",
    context: "GraphContext",
    *,
    extra_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a ToolCallNode's arguments against the GraphContext.

    Spec B §2.3 step 1:

    * When ``arguments_template`` is set: render it as Jinja, parse as JSON,
      return the dict. JSON parse failure raises :class:`ValueError` (caller
      maps it to ``ended_detail='template_error'``).
    * Otherwise: walk ``arguments`` recursively. Any string leaf is rendered
      as a Jinja template against GraphContext; non-string leaves pass through
      unchanged.

    ``extra_scope`` carries the per-fan-out-instance vars (``fanout_index``,
    ``fanout_item``) when this ToolCall node is a fan-out target (Spec B §2.1).
    Without it, a ``map``/``broadcast`` ToolCall target referencing
    ``{{ fanout_item.* }}`` would hit ``StrictUndefined`` and fail every
    instance with ``template_error`` (mirrors the agent/subgraph node paths,
    which already thread ``extra_scope``).
    """
    from primer.graph.template import render_template_safely

    if node.arguments_template:
        text = render_template_safely(
            node.arguments_template, context, extra_scope=extra_scope
        )
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
            return render_template_safely(
                value, context, extra_scope=extra_scope
            )
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

    Graph *creation* now permits empty / partial drafts (runnability is
    enforced at session-start via :meth:`Graph.assert_runnable`). This is
    defence in depth for the executor: if an unrunnable graph reaches
    ``invoke`` anyway (e.g. a bypassed session-start check, or a graph
    built via ``Graph.model_construct``), surface the same clear,
    enumerated runnability error rather than a bare mid-run ``ValueError``
    (or an ``IndexError`` on ``begins[0]`` for an empty graph).
    """
    begins = [n for n in graph.nodes if isinstance(n, _BeginNode)]
    if len(begins) != 1:
        # Route through the shared runnability check so the caller gets the
        # clear, enumerated message (empty graph / wrong Begin count / …)
        # instead of a bare mid-run ValueError or an IndexError on
        # ``begins[0]``. assert_runnable always raises when the Begin count
        # is wrong; the explicit raise below is an unreachable safety net.
        graph.assert_runnable()
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


@dataclass(frozen=True)
class _GraphTransitionEvent:
    """Node-lifecycle transition event yielded at node ENTER and EXIT.

    Spec §2.6 / plan Task 3.1. The superstep loop yields one of these the
    moment a node is marked RUNNING (``phase='enter'``, ``status=None``) and
    one the moment its ``_NodeDone`` lands (``phase='exit'``, ``status`` is
    ``'completed'`` on success or ``'failed'`` on error). The session-layer
    translator (:func:`primer.session.persistence.translate_stream_event`)
    converts each into a ``SessionMessageRecord(kind=graph_transition,
    payload={node_id, node_kind, phase, status})`` which flows through the
    existing tap unchanged (``record_to_tap_event`` maps it 1:1 onto
    :attr:`primer.tap.event.TapEventClass.GRAPH_TRANSITION`).

    Suspended nodes (an approval-yield park) do NOT emit an exit transition —
    they are parked, not done; the matching exit lands on the resume path.
    """

    node_id: str
    node_kind: str
    phase: str  # "enter" | "exit"
    status: str | None = None  # populated on exit: "completed" | "failed"


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
    tool_name
        The bare name the underlying tool stamped onto its
        :class:`~primer.model.yield_.Yielded` when it suspended. For an
        approval gate this is ``"_approval"`` (re-dispatch with bypass on
        resume). For a **value-yielding** tool (``"ask_user"``) the node's
        result IS the operator's reply, so the resume path runs the tool's
        resume hook on the operator payload rather than re-dispatching.
        ``None`` for a legacy park written before this field existed; the
        resume path treats ``None`` as the bypass-re-dispatch (approval)
        case so old parks keep their byte-identical behaviour.
    resume_metadata
        The ``Yielded.resume_metadata`` the underlying tool stamped (e.g.
        ask_user's ``prompt`` / ``response_schema`` / ``tool_call_id``).
        Surfaced to the channel/REST ask_user prompt + handed to the
        resume hook. Empty for an approval gate.
    """

    node_id: str
    tool_call_id: str
    parked_event_key: str
    arguments: dict[str, Any]
    tool_name: str | None = None
    resume_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PendingAgentYield:
    """One agent node suspended on a yielding tool (ask_user) or an
    approval gate mid-superstep.

    Captured when :class:`YieldToWorker` bubbles up from
    ``_stream_agent_node``; persisted into the checkpoint so a resumed
    executor can rebuild the node's turn and continue it with the
    human's answer / decision injected as the tool result.

    ``frames`` / ``leaf`` are the unified nested-yield extension: when the
    node's agent turn yielded from INSIDE a nested ``system__invoke_agent``
    (or ``invoke_graph``) invocation, the in-flight subagent chain is carried
    here (root-first JSON-able frames + the deeper leaf yield). Both stay
    EMPTY / ``None`` for the ordinary single-event park (the node's own
    ask_user / approval gate), so that path is byte-identical. On resume the
    worker runs the continuation walk over ``frames`` + ``leaf`` to unwind the
    subagent before delivering the result into this node.
    """

    node_id: str
    tool_call_id: str
    event_key: str
    tool_name: str               # "ask_user" or "_approval"
    resume_metadata: dict[str, Any]
    llm_messages: list[dict[str, Any]]
    iteration: int
    frames: list[dict[str, Any]] = field(default_factory=list)
    leaf: dict[str, Any] | None = None

