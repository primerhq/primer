"""Executor's initial ready set seeds from the unique Begin node.

The new topology rules forbid zero-Begin graphs at construction time, so
the only valid case is the happy path; the multi-Begin guard remains as
defence in depth against bypassed validators (e.g. ``model_construct``).
"""

from __future__ import annotations

import pytest

from primer.graph.base import _resolve_initial_ready_node
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)


def test_seeds_from_begin_when_present() -> None:
    g = Graph(
        id="g",
        description="t",
        nodes=[
            _BeginNode(id="start"),
            _AgentNodeRef(id="a", agent_id="ag"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="start", to_node="a"),
            _StaticEdge(from_node="a", to_node="end"),
        ],
        entry_node_id="start",
    )
    assert _resolve_initial_ready_node(g) == "start"


def test_raises_when_multiple_begin_nodes() -> None:
    """Spec topology rule §1.5 enforces exactly one Begin; this guard
    is defence in depth in case the validator is bypassed."""
    g = Graph.model_construct(
        id="g",
        description="t",
        nodes=[
            _BeginNode(id="b1"),
            _BeginNode(id="b2"),
            _EndNode(id="e"),
        ],
        edges=[_StaticEdge(from_node="b1", to_node="e")],
        entry_node_id="b1",
    )
    with pytest.raises(ValueError):
        _resolve_initial_ready_node(g)
