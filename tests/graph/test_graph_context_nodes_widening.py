"""GraphContext.nodes accepts both NodeOutput AND list[NodeOutput]
(Spec B §1.2 — fan-out targets surface as the indexed aggregator list)."""

from __future__ import annotations

from primer.model.graph import GraphContext, NodeOutput


def test_accepts_single_node_output() -> None:
    out = NodeOutput(text="ok", iteration=0)
    ctx = GraphContext(initial_input="x", iteration=0, nodes={"a": out})
    assert isinstance(ctx.nodes["a"], NodeOutput)


def test_accepts_list_of_node_outputs() -> None:
    outs = [NodeOutput(text=f"o{i}", iteration=0) for i in range(3)]
    ctx = GraphContext(initial_input="x", iteration=0, nodes={"workers": outs})
    assert isinstance(ctx.nodes["workers"], list)
    assert len(ctx.nodes["workers"]) == 3


def test_mixed_single_and_list() -> None:
    ctx = GraphContext(
        initial_input="x",
        iteration=0,
        nodes={
            "begin": NodeOutput(text="seed", iteration=0),
            "workers": [
                NodeOutput(text="w0", iteration=0),
                NodeOutput(text="w1", iteration=0),
            ],
            "workers[0]": NodeOutput(text="w0", iteration=0),
            "workers[1]": NodeOutput(text="w1", iteration=0),
        },
    )
    assert isinstance(ctx.nodes["begin"], NodeOutput)
    assert isinstance(ctx.nodes["workers"], list)
    assert isinstance(ctx.nodes["workers[0]"], NodeOutput)
