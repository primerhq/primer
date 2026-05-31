"""Spec §1.5: exactly one Begin, >=1 End, Begin has no incoming, End
has no outgoing, every End reachable from Begin."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)


def _g(nodes, edges, **overrides):
    base = dict(id="g", description="t", nodes=nodes, edges=edges)
    # entry_node_id is still a required Pydantic field at this point in
    # the migration; pin it to the first node id so Pydantic accepts the
    # construction and the topology validator (the thing under test)
    # runs. Phase 7.2 removes the field entirely.
    base.setdefault("entry_node_id", nodes[0].id)
    base.update(overrides)
    return Graph(**base)


def test_minimal_valid_begin_end_graph() -> None:
    _g(
        [_BeginNode(id="b"), _EndNode(id="e")],
        [_StaticEdge(from_node="b", to_node="e")],
    )


def test_rejects_zero_begin() -> None:
    with pytest.raises(ValidationError):
        _g(
            [_AgentNodeRef(id="a", agent_id="ag"), _EndNode(id="e")],
            [_StaticEdge(from_node="a", to_node="e")],
        )


def test_rejects_two_begin() -> None:
    with pytest.raises(ValidationError):
        _g(
            [_BeginNode(id="b1"), _BeginNode(id="b2"), _EndNode(id="e")],
            [_StaticEdge(from_node="b1", to_node="e")],
        )


def test_rejects_zero_end() -> None:
    with pytest.raises(ValidationError):
        _g(
            [_BeginNode(id="b"), _AgentNodeRef(id="a", agent_id="ag")],
            [_StaticEdge(from_node="b", to_node="a")],
        )


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


def test_rejects_unreachable_end() -> None:
    with pytest.raises(ValidationError):
        _g(
            [_BeginNode(id="b"), _EndNode(id="e1"), _EndNode(id="e_orphan")],
            [_StaticEdge(from_node="b", to_node="e1")],
        )


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
