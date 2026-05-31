"""Spec B §1.3 topology rules: FanOut has no outgoing edges; FanOut spec
targets exist; FanOut targets are not Begin or another FanOut; map source
not a fan-out target; FanIn >=1 incoming; reachability through FanOut
implicit edges."""

import pytest
from pydantic import ValidationError

from primer.model.graph import (
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _StaticEdge,
    FanOutSpec,
    Graph,
)


def _g(nodes, edges):
    return Graph(id="g", description="t", nodes=nodes, edges=edges)


def test_fanout_no_outgoing_edges_allowed() -> None:
    """FanOut targets live on specs - outgoing edges are forbidden."""
    with pytest.raises(ValidationError):
        _g(
            [
                _BeginNode(id="b"),
                _FanOutNode(id="fan", specs=[
                    FanOutSpec(kind="broadcast", target_node_id="w", count=2),
                ]),
                _AgentNodeRef(id="w", agent_id="ag"),
                _EndNode(id="e"),
            ],
            [
                _StaticEdge(from_node="b", to_node="fan"),
                _StaticEdge(from_node="fan", to_node="w"),  # forbidden
                _StaticEdge(from_node="w", to_node="e"),
            ],
        )


def test_fanout_spec_target_must_exist() -> None:
    with pytest.raises(ValidationError):
        _g(
            [
                _BeginNode(id="b"),
                _FanOutNode(id="fan", specs=[
                    FanOutSpec(kind="broadcast", target_node_id="ghost", count=1),
                ]),
                _EndNode(id="e"),
            ],
            [_StaticEdge(from_node="b", to_node="fan")],
        )


def test_fanout_target_cannot_be_begin() -> None:
    with pytest.raises(ValidationError):
        _g(
            [
                _BeginNode(id="b"),
                _FanOutNode(id="fan", specs=[
                    FanOutSpec(kind="broadcast", target_node_id="b", count=1),
                ]),
                _EndNode(id="e"),
            ],
            [_StaticEdge(from_node="b", to_node="fan")],
        )


def test_fanout_target_cannot_be_another_fanout() -> None:
    with pytest.raises(ValidationError):
        _g(
            [
                _BeginNode(id="b"),
                _FanOutNode(id="f1", specs=[
                    FanOutSpec(kind="broadcast", target_node_id="f2", count=1),
                ]),
                _FanOutNode(id="f2", specs=[
                    FanOutSpec(kind="broadcast", target_node_id="w", count=1),
                ]),
                _AgentNodeRef(id="w", agent_id="ag"),
                _EndNode(id="e"),
            ],
            [
                _StaticEdge(from_node="b", to_node="f1"),
                _StaticEdge(from_node="w", to_node="e"),
            ],
        )


def test_map_source_cannot_be_fanout_target() -> None:
    with pytest.raises(ValidationError):
        _g(
            [
                _BeginNode(id="b"),
                _FanOutNode(id="f1", specs=[
                    FanOutSpec(kind="broadcast", target_node_id="w1", count=2),
                ]),
                _AgentNodeRef(id="w1", agent_id="ag"),
                _FanOutNode(id="f2", specs=[
                    FanOutSpec(
                        kind="map",
                        target_node_id="w2",
                        source_node_id="w1",  # forbidden: w1 is a fan-out target
                        source_path="items",
                    ),
                ]),
                _AgentNodeRef(id="w2", agent_id="ag"),
                _EndNode(id="e"),
            ],
            [
                _StaticEdge(from_node="b", to_node="f1"),
                _StaticEdge(from_node="w1", to_node="f2"),
                _StaticEdge(from_node="w2", to_node="e"),
            ],
        )


def test_fanin_must_have_incoming() -> None:
    with pytest.raises(ValidationError):
        _g(
            [
                _BeginNode(id="b"),
                _FanInNode(id="join"),  # no incoming edge
                _EndNode(id="e"),
            ],
            [_StaticEdge(from_node="b", to_node="e")],
        )


def test_reachability_through_fanout_implicit_edges() -> None:
    """End reachable via FanOut->target->End (no direct edges)."""
    _g(
        [
            _BeginNode(id="b"),
            _FanOutNode(id="fan", specs=[
                FanOutSpec(kind="broadcast", target_node_id="w", count=2),
            ]),
            _AgentNodeRef(id="w", agent_id="ag"),
            _EndNode(id="e"),
        ],
        [
            _StaticEdge(from_node="b", to_node="fan"),
            _StaticEdge(from_node="w", to_node="e"),
        ],
    )
