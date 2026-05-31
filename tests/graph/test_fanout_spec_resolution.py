"""_resolve_fanout_spec walks a single FanOutSpec into the list of
(synthesized_id, target_node_id, fanout_index, fanout_item) tuples that
the executor will dispatch.

Spec B §2.1."""

from __future__ import annotations

import pytest

from primer.model.graph import FanOutSpec, GraphContext, NodeOutput
from primer.graph.base import _resolve_fanout_spec, _FanoutSourceInvalid


def _ctx(nodes: dict) -> GraphContext:
    return GraphContext(initial_input="seed", iteration=0, nodes=nodes)


def test_broadcast_produces_n_instances() -> None:
    spec = FanOutSpec(kind="broadcast", target_node_id="worker", count=3)
    ctx = _ctx({"begin": NodeOutput(text="x", iteration=0)})
    fanout_output = NodeOutput(text="seed", iteration=0)
    rows = _resolve_fanout_spec(spec, ctx, fanout_output)
    assert len(rows) == 3
    assert [r.synthesized_id for r in rows] == ["worker[0]", "worker[1]", "worker[2]"]
    assert [r.target_node_id for r in rows] == ["worker"] * 3
    assert [r.fanout_index for r in rows] == [0, 1, 2]
    # broadcast: fanout_item is the FanOut's own NodeOutput
    assert all(r.fanout_item is fanout_output for r in rows)


def test_tee_produces_one_instance_per_target() -> None:
    spec = FanOutSpec(kind="tee", target_node_ids=["a", "b", "c"])
    rows = _resolve_fanout_spec(spec, _ctx({}), NodeOutput(text="x", iteration=0))
    assert [r.synthesized_id for r in rows] == ["a", "b", "c"]
    assert [r.target_node_id for r in rows] == ["a", "b", "c"]
    # tee: no synthesized index — fanout_index is None
    assert all(r.fanout_index is None for r in rows)


def test_map_resolves_source_list() -> None:
    spec = FanOutSpec(
        kind="map",
        target_node_id="worker",
        source_node_id="planner",
        source_path="topics",
    )
    planner = NodeOutput(
        text="",
        parsed={"topics": ["a", "b", "c"]},
        iteration=0,
    )
    rows = _resolve_fanout_spec(
        spec,
        _ctx({"planner": planner}),
        NodeOutput(text="", iteration=0),
    )
    assert [r.synthesized_id for r in rows] == ["worker[0]", "worker[1]", "worker[2]"]
    assert [r.fanout_item for r in rows] == ["a", "b", "c"]


def test_map_missing_source_raises() -> None:
    spec = FanOutSpec(
        kind="map",
        target_node_id="w",
        source_node_id="planner",
        source_path="topics",
    )
    with pytest.raises(_FanoutSourceInvalid):
        _resolve_fanout_spec(
            spec,
            _ctx({"planner": NodeOutput(text="", parsed={}, iteration=0)}),
            NodeOutput(text="", iteration=0),
        )


def test_map_non_list_source_raises() -> None:
    spec = FanOutSpec(
        kind="map",
        target_node_id="w",
        source_node_id="planner",
        source_path="topic",
    )
    with pytest.raises(_FanoutSourceInvalid):
        _resolve_fanout_spec(
            spec,
            _ctx({"planner": NodeOutput(text="", parsed={"topic": "single"}, iteration=0)}),
            NodeOutput(text="", iteration=0),
        )


def test_map_path_with_bracket_index() -> None:
    spec = FanOutSpec(
        kind="map",
        target_node_id="w",
        source_node_id="planner",
        source_path="groups[0].items",
    )
    planner = NodeOutput(
        text="",
        parsed={"groups": [{"items": ["x", "y"]}]},
        iteration=0,
    )
    rows = _resolve_fanout_spec(
        spec, _ctx({"planner": planner}), NodeOutput(text="", iteration=0)
    )
    assert [r.fanout_item for r in rows] == ["x", "y"]
