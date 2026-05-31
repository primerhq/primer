"""Pydantic models for the agent-graph runtime.

A *graph* is a directed graph of agent nodes (and optionally
sub-graph nodes), connected by static or conditional edges. The
graph executor walks the graph in Pregel-style supersteps; each
node produces a :class:`NodeOutput` consumed by downstream nodes
through user-defined Jinja2 templates.

See ``docs/superpowers/specs/2026-05-03-agent-graph-design.md`` for
the surrounding design.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    model_validator,
)

from primer.model.chat import Message
from primer.model.common import Describeable, Identifiable
from primer.model.workspace_session import SessionStatus


# ===========================================================================
# Node-runtime status (per-node, distinct from the agent-level SessionStatus)
# ===========================================================================


class NodeRuntimeStatus(str, Enum):
    """Per-node lifecycle within a graph execution.

    Distinct from :class:`primer.model.session.SessionStatus` because
    graphs add the ``pending`` (not yet reached) and ``failed``
    (errored out) states that don't apply to standalone agent
    sessions. The graph's :class:`SessionStatus` is aggregated FROM
    these per-node values.
    """

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    ENDED = "ended"
    FAILED = "failed"


class NodeRuntimeState(BaseModel):
    """Per-node runtime state within a graph execution."""

    status: NodeRuntimeStatus = Field(
        default=NodeRuntimeStatus.PENDING,
        description="Current per-node status.",
    )
    last_run_iteration: int | None = Field(
        default=None,
        ge=0,
        description="Most recent graph iteration that ran this node, if any.",
    )
    last_run_at: datetime | None = Field(
        default=None,
        description="UTC instant of the most recent run.",
    )
    error: str | None = Field(
        default=None,
        description="Error message when ``status == FAILED``.",
    )


# ===========================================================================
# NodeOutput + GraphContext (Jinja-rendering + router-input shape)
# ===========================================================================


class NodeOutput(BaseModel):
    """One previously-executed node's contribution to ``GraphContext``."""

    text: str = Field(
        ...,
        description=(
            "Concatenated text from the node's last assistant "
            "message. Empty string if the assistant produced only "
            "tool calls or non-text parts."
        ),
    )
    parsed: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Parsed structured output (``json.loads`` of ``text``) "
            "when the node had ``response_format`` set; ``None`` "
            "otherwise."
        ),
    )
    history: list[Message] = Field(
        default_factory=list,
        description=(
            "Full message history for this node up to and including "
            "the most recent invocation."
        ),
    )
    iteration: int = Field(
        ...,
        ge=0,
        description="Graph iteration that produced this output.",
    )


class GraphContext(BaseModel):
    """Rendering context passed to Jinja2 templates AND callable routers."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    initial_input: Any = Field(
        ...,
        description=(
            "The graph's initial input. Historically a ``list[Message]`` "
            "(the messages passed to ``invoke()``); spec §4.3 widens "
            "this to ``Any`` so the workspace executor can seed it from "
            "``session.metadata['graph_input']`` (dict / str / list / "
            "any JSON-serialisable value). Begin-firing code branches "
            "on the runtime type to materialise the right NodeOutput."
        ),
    )
    iteration: int = Field(
        ...,
        ge=0,
        description="Current graph iteration (0 on entry-node execution).",
    )
    nodes: dict[str, NodeOutput] = Field(
        default_factory=dict,
        description=(
            "Already-executed nodes keyed by node id. Each entry's "
            "``text`` / ``parsed`` / ``history`` is the most-recent "
            "result for that node (cycles overwrite)."
        ),
    )


# ===========================================================================
# GraphNode discriminated union
# ===========================================================================


_DEFAULT_INPUT_TEMPLATE = (
    "{% for m in initial_input %}{{ m.parts[0].text }}\n{% endfor %}"
)


class _AgentNodeRef(BaseModel):
    """Node that runs a single :class:`primer.model.agent.Agent`."""

    kind: Literal["agent"] = Field(
        default="agent",
        description="Discriminator tag identifying this node as an agent reference.",
    )
    id: str = Field(
        ...,
        min_length=1,
        description="Within-graph unique node id (e.g. 'researcher_1').",
    )
    agent_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the stored Agent this node executes.",
    )
    input_template: str = Field(
        default=_DEFAULT_INPUT_TEMPLATE,
        description=(
            "Jinja2 template rendered against :class:`GraphContext` "
            "to produce the user-role text appended to this node's "
            "history before invocation. Default concatenates the "
            "graph's initial input verbatim."
        ),
    )
    response_format: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional JSON Schema forwarded to the agent's "
            "``invoke(response_format=...)``. When set, the agent "
            "produces structured output and ``NodeOutput.parsed`` "
            "is populated. When ``None`` (default), the agent runs "
            "unconstrained and downstream nodes receive only the "
            "raw text + history."
        ),
    )
    description: str | None = Field(
        default=None,
        description="Free-form human-readable label for the UI.",
    )
    input_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Designer metadata. After Jinja renders ``input_template``, "
            "if the result parses as JSON, the executor soft-validates "
            "against this schema and logs a WARNING on mismatch; never "
            "fails the node. Intent is UI assistance for template-"
            "building, not a runtime gate."
        ),
    )


class _BeginNode(BaseModel):
    """Entry-point node — pure data-shaping, no LLM call.

    Carries the graph's input contract. When ``input_schema`` is set,
    the session-create handler validates ``graph_input`` against it
    before the worker dispatches the graph.
    """

    kind: Literal["begin"] = "begin"
    id: str = Field(..., min_length=1)
    description: str | None = Field(
        default=None,
        description="Free-form human-readable label for the UI.",
    )
    input_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional JSON Schema 2020-12 describing the graph's input. "
            "When set, the session-create handler validates ``graph_input`` "
            "against this schema; failure returns 422. When unset, the "
            "graph accepts any input shape (string, list[Message], dict)."
        ),
    )


class _GraphNodeRef(BaseModel):
    """Node that delegates to a sub-graph (recursive composition)."""

    kind: Literal["graph"] = Field(
        default="graph",
        description="Discriminator tag identifying this node as a sub-graph reference.",
    )
    id: str = Field(..., min_length=1)
    graph_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the stored Graph this node delegates to.",
    )
    input_template: str = Field(
        default=_DEFAULT_INPUT_TEMPLATE,
        description=(
            "Jinja2 template rendered to produce the sub-graph's "
            "``initial_input`` (a single user-role :class:`Message`)."
        ),
    )
    description: str | None = Field(
        default=None,
        description="Free-form human-readable label for the UI.",
    )


class _TerminalNode(BaseModel):
    """Sink node. Reaching one terminates the graph successfully."""

    kind: Literal["terminal"] = Field(
        default="terminal",
        description="Discriminator tag identifying this node as a terminal sink.",
    )
    id: str = Field(..., min_length=1)


class _EndNode(BaseModel):
    """Sink node carrying the graph's output contract.

    Pure data-shaping — when reached, renders ``output_template`` over
    the current ``GraphContext`` to produce the graph's final output.
    """

    kind: Literal["end"] = "end"
    id: str = Field(..., min_length=1)
    description: str | None = None
    output_template: str = Field(
        default="",
        description=(
            "Jinja2 template rendered over the GraphContext when End "
            "fires. The rendered string becomes the graph's final "
            "output. An empty template terminates the graph without an "
            "output payload."
        ),
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional JSON Schema 2020-12. When set, the rendered "
            "output_template MUST parse as JSON conforming to this "
            "schema; failure ends the graph with "
            "ended_detail='end_output_invalid'."
        ),
    )


GraphNode = Annotated[
    Union[_AgentNodeRef, _GraphNodeRef, _TerminalNode, _BeginNode, _EndNode],
    Field(discriminator="kind"),
]


# ===========================================================================
# Router discriminated union
# ===========================================================================


class BranchCondition(BaseModel):
    """One predicate inside a JsonPathRouter branch.

    Resolves ``path`` against the source node's ``NodeOutput.parsed``
    via dotted-segment + bracket-index walking, then applies ``op``.

    Missing-path rule: when the path doesn't resolve, EVERY operator
    returns False — including ``ne`` and ``not_in``. Use ``exists`` to
    test presence.
    """

    path: str = Field(
        ...,
        description=(
            "Dotted path with bracket indexing (`a.b[2].c`) into "
            "NodeOutput.parsed of the conditional edge's source node."
        ),
    )
    op: Literal[
        "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists"
    ]
    value: Any | None = Field(
        default=None,
        description=(
            "Operand on the right of the comparison. Unused for `exists`. "
            "For `in`/`not_in` MUST be a list."
        ),
    )


class JsonPathBranch(BaseModel):
    """One branch of a JsonPathRouter.

    All ``conditions`` must hold (AND). An empty list matches
    everything — useful as a catch-all branch placed at the end of the
    branches list.
    """

    conditions: list[BranchCondition] = Field(default_factory=list)
    to_node: str = Field(
        ...,
        min_length=1,
        description="Target node id when this branch matches.",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_when(cls, data: Any) -> Any:
        """Translate the legacy ``when: dict[str, Any]`` AND-of-equality
        shape into ``conditions`` so existing graph fixtures still
        deserialise during the migration phase.

        Phase 6 cleanup deletes this validator and the
        ``test_jsonpath_branch_legacy_when_still_accepted_for_now``
        test together.
        """
        if not isinstance(data, dict):
            return data
        legacy_when = data.get("when")
        if legacy_when is None or "conditions" in data:
            return data
        data = dict(data)
        data["conditions"] = [
            {"path": path, "op": "eq", "value": expected}
            for path, expected in legacy_when.items()
        ]
        data.pop("when")
        return data


class _JsonPathRouter(BaseModel):
    """Routes by JSON-path matching against the source's parsed output.

    Requires the source node to have ``response_format`` set so
    :attr:`NodeOutput.parsed` is populated. Branches are evaluated
    in order; first match wins. ``default_to`` fires when no
    branch matches; if ``None`` and no branch matches the graph
    terminates with ``ended_reason='failed'``.
    """

    kind: Literal["json_path"] = Field(
        default="json_path",
        description="Discriminator tag identifying this router as JSON-path-based.",
    )
    branches: list[JsonPathBranch] = Field(
        ...,
        min_length=1,
        description="Ordered branches; first match wins.",
    )
    default_to: str | None = Field(
        default=None,
        description=(
            "Fallback target when no branch matches. ``None`` means "
            "the graph terminates with ``ended_reason='failed'``."
        ),
    )


class _CallableRouter(BaseModel):
    """Routes via a registered Python callable.

    ``callable_id`` is resolved at run time against the executor's
    :class:`primer.graph.RouterRegistry`. The callable signature is
    ``(context: GraphContext, source: NodeOutput) -> str`` (sync or
    async); the returned string MUST be the id of an existing node.
    """

    kind: Literal["callable"] = Field(
        default="callable",
        description="Discriminator tag identifying this router as callable-backed.",
    )
    callable_id: str = Field(
        ...,
        min_length=1,
        description="Lookup key in the executor's RouterRegistry.",
    )


GraphRouter = Annotated[
    Union[_JsonPathRouter, _CallableRouter],
    Field(discriminator="kind"),
]


# ===========================================================================
# GraphEdge discriminated union
# ===========================================================================


class _StaticEdge(BaseModel):
    """Unconditional edge: always fires from ``from_node`` to ``to_node``."""

    kind: Literal["static"] = Field(
        default="static",
        description="Discriminator tag identifying this edge as static.",
    )
    from_node: str = Field(..., min_length=1)
    to_node: str = Field(..., min_length=1)


class _ConditionalEdge(BaseModel):
    """Router-driven edge: the router decides the destination."""

    kind: Literal["conditional"] = Field(
        default="conditional",
        description="Discriminator tag identifying this edge as conditional.",
    )
    from_node: str = Field(..., min_length=1)
    router: GraphRouter = Field(
        ...,
        description="Router resolving the next node from GraphContext.",
    )


GraphEdge = Annotated[
    Union[_StaticEdge, _ConditionalEdge],
    Field(discriminator="kind"),
]


# ===========================================================================
# Graph
# ===========================================================================


class Graph(Describeable):
    """A directed graph of agent nodes (and optionally sub-graph nodes).

    Inherits ``id`` and ``description`` from :class:`Describeable`.
    Persisted via :class:`primer.int.Storage` with model class
    ``Graph``.

    Cyclic graphs MUST set ``max_iterations`` to bound execution;
    otherwise a stuck cycle runs unbounded. Acyclic graphs may leave
    it ``None`` (the executor still terminates on terminal nodes /
    dead ends).
    """

    nodes: list[GraphNode] = Field(
        ...,
        min_length=1,
        description="Discriminated union of agent / subgraph / terminal nodes.",
    )
    edges: list[GraphEdge] = Field(
        default_factory=list,
        description="Static or conditional edges connecting nodes.",
    )
    entry_node_id: str = Field(
        ...,
        min_length=1,
        description="Id of the first node executed when invoke() is called.",
    )
    max_iterations: PositiveInt | None = Field(
        default=None,
        description=(
            "Hard cap on supersteps. ``None`` means unbounded. "
            "Recommended for any graph that contains a cycle."
        ),
    )
    harness_id: str | None = Field(
        default=None,
        description=(
            "When set, this row is managed by the named harness. "
            "Mutation through the public CRUD endpoints returns 409 — "
            "use the harness's sync/uninstall flow instead."
        ),
    )

    @model_validator(mode="after")
    def _validate_topology(self) -> "Graph":
        node_ids = [n.id for n in self.nodes]
        # Unique node ids.
        seen: set[str] = set()
        for nid in node_ids:
            if nid in seen:
                raise ValueError(
                    f"duplicate node id {nid!r}; node ids must be unique within a graph"
                )
            seen.add(nid)
        # entry_node_id is in nodes.
        if self.entry_node_id not in seen:
            raise ValueError(
                f"entry_node_id {self.entry_node_id!r} does not match any node id"
            )
        # Every edge endpoint exists. ConditionalEdge router branch
        # `to_node` is also validated.
        for edge in self.edges:
            if edge.from_node not in seen:
                raise ValueError(
                    f"edge.from_node {edge.from_node!r} does not match any node id"
                )
            if isinstance(edge, _StaticEdge):
                if edge.to_node not in seen:
                    raise ValueError(
                        f"edge.to_node {edge.to_node!r} does not match any node id"
                    )
            else:  # _ConditionalEdge
                router = edge.router
                if isinstance(router, _JsonPathRouter):
                    for branch in router.branches:
                        if branch.to_node not in seen:
                            raise ValueError(
                                f"branch.to_node {branch.to_node!r} does not match any node id"
                            )
                    if (
                        router.default_to is not None
                        and router.default_to not in seen
                    ):
                        raise ValueError(
                            f"router.default_to {router.default_to!r} does not match any node id"
                        )
                # _CallableRouter target is resolved at run time.
        return self


# ===========================================================================
# GraphThread + GraphNodeMessage (storage-backed runtime; consumed by G2)
# ===========================================================================


class GraphThread(Identifiable):
    """One execution of one graph (standalone, storage-backed).

    Persisted via :class:`primer.int.Storage` with model class
    ``GraphThread``. The G2 sub-project introduces this as the
    parent row for per-node :class:`GraphNodeMessage` rows; the
    type lives here so the model layer is self-contained.
    """

    graph_id: str = Field(..., min_length=1)
    title: str | None = Field(default=None)
    created_at: datetime = Field(...)
    last_activity_at: datetime = Field(...)
    iteration: int = Field(default=0, ge=0)
    node_states: dict[str, NodeRuntimeState] = Field(default_factory=dict)
    status: SessionStatus = Field(
        default=SessionStatus.RUNNING,
        description="Aggregated graph status; derived from per-node statuses.",
    )
    ended_reason: Literal[
        "completed",
        "failed",
        "cancelled",
        "max_iterations_exceeded",
    ] | None = Field(
        default=None,
        description="Set when ``status == ENDED``.",
    )
    ended_detail: str | None = Field(
        default=None,
        description=(
            "Spec §5.4 failure code (e.g. ``end_output_invalid``, "
            "``template_error``, ``routing_failed``, "
            "``max_iterations_exceeded``) carried alongside "
            "``ended_reason='failed'``. ``None`` for successful "
            "completions and for ``cancelled``."
        ),
    )


class GraphNodeMessage(Identifiable):
    """One message persisted under a :class:`GraphThread`'s node.

    Parallel to :class:`primer.model.thread.ThreadMessage` but
    additionally scoped by ``node_id`` so a single graph thread
    holds many independent message histories (one per node).
    """

    graph_thread_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    role: Literal["user", "assistant", "system", "tool"] = Field(...)
    parts: list[Any] = Field(
        ...,
        min_length=1,
        description=(
            "List of :class:`primer.model.chat.Part` instances. Typed "
            "as ``list[Any]`` here to avoid a circular import of the "
            "Part union; downstream consumers re-cast via "
            "``Message(role=..., parts=row.parts)``."
        ),
    )
    created_at: datetime = Field(...)
    iteration: int = Field(..., ge=0)
    sequence: int = Field(
        ...,
        ge=0,
        description=(
            "Per-(graph_thread, node) monotonic sequence; secondary "
            "sort key after ``iteration`` for cursor-paginated history."
        ),
    )


__all__ = [
    "BranchCondition",
    "Graph",
    "GraphContext",
    "GraphEdge",
    "GraphNode",
    "GraphNodeMessage",
    "GraphRouter",
    "GraphThread",
    "JsonPathBranch",
    "NodeOutput",
    "NodeRuntimeState",
    "NodeRuntimeStatus",
]
