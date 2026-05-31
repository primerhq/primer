"""Agent and subgraph nodes carry optional description + input_schema metadata."""

from __future__ import annotations

from primer.model.graph import _AgentNodeRef, _GraphNodeRef


def test_agent_node_optional_metadata_defaults() -> None:
    n = _AgentNodeRef(id="n", agent_id="ag")
    assert n.description is None
    assert n.input_schema is None


def test_agent_node_with_description_and_input_schema() -> None:
    n = _AgentNodeRef(
        id="n",
        agent_id="ag",
        description="Summarise the prior research",
        input_schema={"type": "object", "properties": {"research": {"type": "string"}}},
    )
    assert n.description == "Summarise the prior research"
    assert n.input_schema["type"] == "object"


def test_subgraph_node_optional_description() -> None:
    n = _GraphNodeRef(id="n", graph_id="g")
    assert n.description is None


def test_subgraph_node_with_description() -> None:
    n = _GraphNodeRef(id="n", graph_id="g", description="Critic loop")
    assert n.description == "Critic loop"
