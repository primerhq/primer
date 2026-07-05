"""Graph creation vs. runnability split.

Graph *construction* (persist-time ``_validate_topology``) permits empty
and partial/incomplete graphs — those are valid drafts. Only *referential*
integrity is enforced at construction (edge endpoints reference existing
node ids, unique node ids, ``on_max_iterations`` target exists).

Whether a graph can actually run — exactly one Begin, at least one End,
End reachable from Begin, bounded loops — is a separate *runnability*
invariant enforced at session-start via :meth:`Graph.assert_runnable`.
"""

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
    base.update(overrides)
    return Graph(**base)


# ---------------------------------------------------------------------------
# Construction now tolerates empty / partial graphs.
# ---------------------------------------------------------------------------


def test_empty_graph_constructs() -> None:
    """An empty graph (no nodes, no edges) is a valid draft."""
    g = _g([], [])
    assert g.nodes == []
    assert g.edges == []


def test_partial_graph_without_begin_or_end_constructs() -> None:
    """A single agent node with no Begin/End still constructs."""
    g = _g([_AgentNodeRef(id="a", agent_id="ag")], [])
    assert len(g.nodes) == 1


def test_empty_graph_round_trips_through_model_dump() -> None:
    g = _g([], [])
    reloaded = Graph.model_validate(g.model_dump())
    assert reloaded.nodes == []


# ---------------------------------------------------------------------------
# Referential integrity is STILL enforced at construction.
# ---------------------------------------------------------------------------


def test_edge_referencing_missing_node_still_raises_at_construction() -> None:
    with pytest.raises(ValidationError):
        _g(
            [_BeginNode(id="b"), _EndNode(id="e")],
            [_StaticEdge(from_node="b", to_node="ghost")],
        )


def test_duplicate_node_ids_still_raise_at_construction() -> None:
    with pytest.raises(ValidationError):
        _g([_BeginNode(id="x"), _EndNode(id="x")], [])


def test_on_max_iterations_unknown_target_still_raises_at_construction() -> None:
    with pytest.raises(ValidationError):
        _g(
            [_BeginNode(id="b"), _EndNode(id="e")],
            [_StaticEdge(from_node="b", to_node="e")],
            max_iterations=3,
            on_max_iterations="nope",
        )


# ---------------------------------------------------------------------------
# assert_runnable() enforces the runnability invariants.
# ---------------------------------------------------------------------------


def test_assert_runnable_raises_for_empty_graph() -> None:
    g = _g([], [])
    with pytest.raises(ValueError):
        g.assert_runnable()


def test_assert_runnable_raises_when_no_begin() -> None:
    g = _g(
        [_AgentNodeRef(id="a", agent_id="ag"), _EndNode(id="e")],
        [_StaticEdge(from_node="a", to_node="e")],
    )
    with pytest.raises(ValueError):
        g.assert_runnable()


def test_assert_runnable_raises_when_no_end() -> None:
    g = _g(
        [_BeginNode(id="b"), _AgentNodeRef(id="a", agent_id="ag")],
        [_StaticEdge(from_node="b", to_node="a")],
    )
    with pytest.raises(ValueError):
        g.assert_runnable()


def test_assert_runnable_raises_when_end_unreachable() -> None:
    g = _g(
        [_BeginNode(id="b"), _EndNode(id="e1"), _EndNode(id="orphan")],
        [_StaticEdge(from_node="b", to_node="e1")],
    )
    with pytest.raises(ValueError) as exc:
        g.assert_runnable()
    assert "orphan" in str(exc.value)


def test_assert_runnable_passes_for_valid_begin_agent_end() -> None:
    g = _g(
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
    # Does not raise.
    g.assert_runnable()
