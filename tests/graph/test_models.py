"""Tests for primer.model.graph (Graph + nodes + edges + routers)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from primer.model.chat import (
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    _GraphNodeEvent,
)
from primer.model.graph import (
    Graph,
    GraphContext,
    GraphEdge,
    GraphNode,
    GraphNodeMessage,
    GraphThread,
    JsonPathBranch,
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


# ===========================================================================
# Node + edge + router round-trips
# ===========================================================================


class TestNodeUnionDiscrimination:
    def test_agent_node_round_trip(self) -> None:
        node = _AgentNodeRef(id="A", agent_id="researcher")
        adapter: TypeAdapter[GraphNode] = TypeAdapter(GraphNode)
        parsed = adapter.validate_python(node.model_dump())
        assert isinstance(parsed, _AgentNodeRef)
        assert parsed.agent_id == "researcher"
        assert parsed.response_format is None

    def test_graph_node_round_trip(self) -> None:
        node = _GraphNodeRef(id="sub", graph_id="inner-graph")
        adapter: TypeAdapter[GraphNode] = TypeAdapter(GraphNode)
        parsed = adapter.validate_python(node.model_dump())
        assert isinstance(parsed, _GraphNodeRef)
        assert parsed.graph_id == "inner-graph"

    def test_terminal_node_round_trip(self) -> None:
        node = _TerminalNode(id="exit")
        adapter: TypeAdapter[GraphNode] = TypeAdapter(GraphNode)
        parsed = adapter.validate_python(node.model_dump())
        assert isinstance(parsed, _TerminalNode)


class TestEdgeUnionDiscrimination:
    def test_static_edge_round_trip(self) -> None:
        e = _StaticEdge(from_node="A", to_node="B")
        adapter: TypeAdapter[GraphEdge] = TypeAdapter(GraphEdge)
        parsed = adapter.validate_python(e.model_dump())
        assert isinstance(parsed, _StaticEdge)

    def test_conditional_edge_with_jsonpath_router(self) -> None:
        e = _ConditionalEdge(
            from_node="D",
            router=_JsonPathRouter(
                branches=[JsonPathBranch(when={"next": "exit"}, to_node="exit")],
            ),
        )
        adapter: TypeAdapter[GraphEdge] = TypeAdapter(GraphEdge)
        parsed = adapter.validate_python(e.model_dump())
        assert isinstance(parsed, _ConditionalEdge)
        assert isinstance(parsed.router, _JsonPathRouter)

    def test_conditional_edge_with_callable_router(self) -> None:
        e = _ConditionalEdge(
            from_node="D",
            router=_CallableRouter(callable_id="my_router"),
        )
        adapter: TypeAdapter[GraphEdge] = TypeAdapter(GraphEdge)
        parsed = adapter.validate_python(e.model_dump())
        assert isinstance(parsed, _ConditionalEdge)
        assert isinstance(parsed.router, _CallableRouter)


# ===========================================================================
# Graph topology validation
# ===========================================================================


class TestGraphTopology:
    def test_minimal_valid_graph(self) -> None:
        g = Graph(
            id="g-1",
            description="trivial",
            nodes=[_AgentNodeRef(id="A", agent_id="agent-x")],
            entry_node_id="A",
        )
        assert g.entry_node_id == "A"
        assert g.max_iterations is None

    def test_duplicate_node_ids_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate node id"):
            Graph(
                id="g-1",
                description="dup",
                nodes=[
                    _AgentNodeRef(id="A", agent_id="x"),
                    _AgentNodeRef(id="A", agent_id="y"),
                ],
                entry_node_id="A",
            )

    def test_entry_node_id_must_exist(self) -> None:
        with pytest.raises(ValidationError, match="entry_node_id"):
            Graph(
                id="g-1",
                description="bad-entry",
                nodes=[_AgentNodeRef(id="A", agent_id="x")],
                entry_node_id="not-a-node",
            )

    def test_static_edge_endpoints_must_exist(self) -> None:
        with pytest.raises(ValidationError, match="edge.to_node"):
            Graph(
                id="g-1",
                description="bad-edge",
                nodes=[_AgentNodeRef(id="A", agent_id="x")],
                edges=[_StaticEdge(from_node="A", to_node="ghost")],
                entry_node_id="A",
            )

    def test_jsonpath_branch_targets_validated(self) -> None:
        with pytest.raises(ValidationError, match="branch.to_node"):
            Graph(
                id="g-1",
                description="bad-branch",
                nodes=[
                    _AgentNodeRef(id="A", agent_id="x"),
                    _AgentNodeRef(id="B", agent_id="y"),
                ],
                edges=[
                    _ConditionalEdge(
                        from_node="A",
                        router=_JsonPathRouter(
                            branches=[
                                JsonPathBranch(when={"x": 1}, to_node="ghost"),
                            ],
                        ),
                    ),
                ],
                entry_node_id="A",
            )

    def test_jsonpath_default_to_validated(self) -> None:
        with pytest.raises(ValidationError, match="router.default_to"):
            Graph(
                id="g-1",
                description="bad-default",
                nodes=[_AgentNodeRef(id="A", agent_id="x")],
                edges=[
                    _ConditionalEdge(
                        from_node="A",
                        router=_JsonPathRouter(
                            branches=[JsonPathBranch(when={"x": 1}, to_node="A")],
                            default_to="ghost",
                        ),
                    ),
                ],
                entry_node_id="A",
            )

    def test_callable_router_target_not_validated_at_construction(self) -> None:
        # Callable routers' targets are checked at run time when the
        # callable returns; construction succeeds.
        g = Graph(
            id="g-1",
            description="callable-edge",
            nodes=[_AgentNodeRef(id="A", agent_id="x")],
            edges=[
                _ConditionalEdge(
                    from_node="A",
                    router=_CallableRouter(callable_id="my_router"),
                ),
            ],
            entry_node_id="A",
        )
        assert g.id == "g-1"

    def test_round_trip_full_example(self) -> None:
        g = Graph(
            id="my-loop",
            description="A -> (B, C) -> D -> (A or exit)",
            entry_node_id="A",
            max_iterations=10,
            nodes=[
                _AgentNodeRef(id="A", agent_id="researcher"),
                _AgentNodeRef(id="B", agent_id="reviewer"),
                _AgentNodeRef(id="C", agent_id="reviewer"),
                _AgentNodeRef(
                    id="D",
                    agent_id="judge",
                    response_format={
                        "type": "object",
                        "properties": {"next_action": {"type": "string"}},
                    },
                ),
                _TerminalNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="A", to_node="B"),
                _StaticEdge(from_node="A", to_node="C"),
                _StaticEdge(from_node="B", to_node="D"),
                _StaticEdge(from_node="C", to_node="D"),
                _ConditionalEdge(
                    from_node="D",
                    router=_JsonPathRouter(
                        branches=[
                            JsonPathBranch(when={"next_action": "retry"}, to_node="A"),
                            JsonPathBranch(when={"next_action": "exit"}, to_node="exit"),
                        ],
                    ),
                ),
            ],
        )
        parsed = Graph.model_validate_json(g.model_dump_json())
        assert parsed == g


# ===========================================================================
# NodeOutput + GraphContext
# ===========================================================================


class TestNodeOutput:
    def test_construction(self) -> None:
        out = NodeOutput(
            text="hello",
            parsed={"next": "exit"},
            history=[Message(role="assistant", parts=[TextPart(text="hello")])],
            iteration=2,
        )
        assert out.text == "hello"
        assert out.parsed == {"next": "exit"}
        assert out.iteration == 2

    def test_negative_iteration_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NodeOutput(text="x", iteration=-1)


class TestGraphContext:
    def test_construction(self) -> None:
        msg = Message(role="user", parts=[TextPart(text="hi")])
        ctx = GraphContext(
            initial_input=[msg],
            iteration=0,
            nodes={"A": NodeOutput(text="ok", iteration=0)},
        )
        assert ctx.initial_input == [msg]
        assert ctx.nodes["A"].text == "ok"


# ===========================================================================
# NodeRuntimeState
# ===========================================================================


class TestNodeRuntimeState:
    def test_default_pending(self) -> None:
        s = NodeRuntimeState()
        assert s.status == NodeRuntimeStatus.PENDING
        assert s.last_run_iteration is None
        assert s.error is None

    def test_failed_with_error(self) -> None:
        s = NodeRuntimeState(status=NodeRuntimeStatus.FAILED, error="boom")
        assert s.status == NodeRuntimeStatus.FAILED
        assert s.error == "boom"


# ===========================================================================
# GraphThread + GraphNodeMessage
# ===========================================================================


class TestGraphThread:
    def test_construction(self) -> None:
        t = GraphThread(
            id="gt-1",
            graph_id="my-loop",
            title="example",
            created_at=datetime.now(timezone.utc),
            last_activity_at=datetime.now(timezone.utc),
        )
        assert t.iteration == 0
        assert t.node_states == {}
        assert t.status == SessionStatus.RUNNING


class TestGraphNodeMessage:
    def test_construction(self) -> None:
        m = GraphNodeMessage(
            id="gnm-1",
            graph_thread_id="gt-1",
            node_id="A",
            role="user",
            parts=[TextPart(text="hi")],
            created_at=datetime.now(timezone.utc),
            iteration=0,
            sequence=0,
        )
        assert m.iteration == 0
        assert m.sequence == 0


# ===========================================================================
# _GraphNodeEvent (round-trip through ExtendedEvent + StreamEvent union)
# ===========================================================================


class TestGraphNodeEvent:
    def test_round_trip_through_stream_event(self) -> None:
        inner = TextDelta(text="hello", index=0)
        ev = _GraphNodeEvent(
            node_id="A",
            iteration=2,
            inner_type="text_delta",
            inner_payload=inner.model_dump(mode="json"),
        )
        wrapped = ExtendedEvent(extended=ev)
        adapter: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)
        parsed = adapter.validate_json(wrapped.model_dump_json())
        assert isinstance(parsed, ExtendedEvent)
        assert isinstance(parsed.extended, _GraphNodeEvent)
        assert parsed.extended.node_id == "A"
        assert parsed.extended.iteration == 2
