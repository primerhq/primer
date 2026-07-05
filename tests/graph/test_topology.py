"""Spec §1.5 topology rules, split across two phases:

* Persist-time (``Graph(...)`` construction) enforces *referential
  integrity* only — unique node ids, edge endpoints reference existing
  nodes, Begin has no incoming edge, End has no outgoing edge.
* *Runnability* invariants — exactly one Begin, >=1 End, every End
  reachable from Begin, bounded loops — are enforced later, at
  session-start, via :meth:`Graph.assert_runnable`. An empty or partial
  graph is a valid draft that constructs fine but fails ``assert_runnable``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _CallableRouter,
    _ConditionalEdge,
    _EndNode,
    _StaticEdge,
)


def _g(nodes, edges, **overrides):
    base = dict(id="g", description="t", nodes=nodes, edges=edges)
    base.update(overrides)
    return Graph(**base)


def test_minimal_valid_begin_end_graph() -> None:
    _g(
        [_BeginNode(id="b"), _EndNode(id="e")],
        [_StaticEdge(from_node="b", to_node="e")],
    )


def test_zero_begin_constructs_but_not_runnable() -> None:
    # Runnability moved to session-start: construction succeeds, but the
    # graph is not runnable (no Begin).
    g = _g(
        [_AgentNodeRef(id="a", agent_id="ag"), _EndNode(id="e")],
        [_StaticEdge(from_node="a", to_node="e")],
    )
    with pytest.raises(ValueError):
        g.assert_runnable()


def test_two_begin_constructs_but_not_runnable() -> None:
    g = _g(
        [_BeginNode(id="b1"), _BeginNode(id="b2"), _EndNode(id="e")],
        [_StaticEdge(from_node="b1", to_node="e")],
    )
    with pytest.raises(ValueError):
        g.assert_runnable()


def test_zero_end_constructs_but_not_runnable() -> None:
    g = _g(
        [_BeginNode(id="b"), _AgentNodeRef(id="a", agent_id="ag")],
        [_StaticEdge(from_node="b", to_node="a")],
    )
    with pytest.raises(ValueError):
        g.assert_runnable()


def test_rejects_incoming_edge_into_begin() -> None:
    with pytest.raises(ValidationError):
        _g(
            [
                _BeginNode(id="b"),
                _AgentNodeRef(id="a", agent_id="ag"),
                _EndNode(id="e"),
            ],
            [
                _StaticEdge(from_node="b", to_node="a"),
                _StaticEdge(from_node="a", to_node="b"),  # bad
                _StaticEdge(from_node="a", to_node="e"),
            ],
        )


def test_rejects_outgoing_edge_from_end() -> None:
    with pytest.raises(ValidationError):
        _g(
            [
                _BeginNode(id="b"),
                _EndNode(id="e"),
                _AgentNodeRef(id="a", agent_id="ag"),
            ],
            [
                _StaticEdge(from_node="b", to_node="e"),
                _StaticEdge(from_node="e", to_node="a"),  # bad
            ],
        )


def test_unreachable_end_constructs_but_not_runnable() -> None:
    g = _g(
        [_BeginNode(id="b"), _EndNode(id="e1"), _EndNode(id="e_orphan")],
        [_StaticEdge(from_node="b", to_node="e1")],
    )
    with pytest.raises(ValueError) as exc:
        g.assert_runnable()
    assert "e_orphan" in str(exc.value)


def test_rejects_unknown_edge_endpoint() -> None:
    with pytest.raises(ValidationError):
        _g(
            [_BeginNode(id="b"), _EndNode(id="e")],
            [_StaticEdge(from_node="b", to_node="does_not_exist")],
        )


def test_rejects_duplicate_node_ids() -> None:
    with pytest.raises(ValidationError):
        _g(
            [_BeginNode(id="x"), _EndNode(id="x")],
            [],
        )


def _cyclic_nodes_edges():
    nodes = [
        _BeginNode(id="b"),
        _AgentNodeRef(id="a1", agent_id="ag"),
        _AgentNodeRef(id="a2", agent_id="ag"),
        _EndNode(id="e"),
    ]
    edges = [
        _StaticEdge(from_node="b", to_node="a1"),
        _StaticEdge(from_node="a1", to_node="a2"),
        _StaticEdge(from_node="a2", to_node="a1"),  # back-edge -> cycle
        _StaticEdge(from_node="a2", to_node="e"),
    ]
    return nodes, edges


def test_cyclic_graph_without_max_iterations_not_runnable() -> None:
    # Construction succeeds (loopability is a runnability rule now); the
    # unbounded cycle is caught at session-start instead.
    nodes, edges = _cyclic_nodes_edges()
    g = _g(nodes, edges)
    with pytest.raises(ValueError):
        g.assert_runnable()


def test_cyclic_graph_with_max_iterations_ok() -> None:
    nodes, edges = _cyclic_nodes_edges()
    _g(nodes, edges, max_iterations=5)


def test_acyclic_linear_graph_without_max_iterations_ok() -> None:
    _g(
        [
            _BeginNode(id="b"),
            _AgentNodeRef(id="a", agent_id="ag"),
            _EndNode(id="e"),
        ],
        [
            _StaticEdge(from_node="b", to_node="a"),
            _StaticEdge(from_node="a", to_node="e"),
        ],
    )


def test_callable_router_without_max_iterations_not_runnable() -> None:
    g = _g(
        [
            _BeginNode(id="b"),
            _AgentNodeRef(id="a", agent_id="ag"),
            _EndNode(id="e"),
        ],
        [
            _StaticEdge(from_node="b", to_node="a"),
            _StaticEdge(from_node="b", to_node="e"),
            _ConditionalEdge(
                from_node="a",
                router=_CallableRouter(callable_id="r"),
            ),
        ],
    )
    with pytest.raises(ValueError):
        g.assert_runnable()
