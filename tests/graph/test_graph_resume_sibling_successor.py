"""AG-H1: a node that finishes in the same superstep a sibling parks in keeps its successors.

The park used to raise before the superstep's next-ready set was computed, and
the resume walks only the resumed nodes' edges, so the finished node's branch
(and every instance of a FanOut in that superstep) silently vanished and the
run ended ``completed``. Inverted from the review probe
``graph/test_probe_sibling_successor.py``.
"""

from __future__ import annotations

import pytest

from primer.graph.base import _GraphEndOutputEvent
from primer.model.graph import (
    FanOutSpec,
    Graph,
    _BeginNode,
    _ConditionalEdge,
    _EndNode,
    _FanOutNode,
    _JsonPathRouter,
    _ToolCallNode,
)
from primer.model.yield_ import YieldToWorker

from tests.graph.test_graph_resume_characterization import _edges, _gate, _Run, _tc, _trace


async def _park_then_resume(graph: Graph, gated: set[str]):
    run = _Run(graph)
    dispatch = _gate(gated)
    _events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    cp = parked.graph_checkpoint
    events, again = await run.resume(dispatch, cp)
    assert again is None
    ends = sorted(e.end_node_id for e in events if isinstance(e, _GraphEndOutputEvent))
    return run, cp, events, ends


@pytest.mark.asyncio
async def test_completed_sibling_successor_runs_after_resume() -> None:
    graph = Graph(
        id="g-sib", description="begin -> {a, b}; a -> enda; b -> endb",
        nodes=[
            _BeginNode(id="begin"), _tc("a"), _tc("b"),
            _EndNode(id="enda", output_template="{{ nodes.a.text }}"),
            _EndNode(id="endb", output_template="{{ nodes.b.text }}"),
        ],
        edges=_edges(("begin", "a"), ("begin", "b"), ("a", "enda"), ("b", "endb")),
    )
    run, cp, events, ends = await _park_then_resume(graph, {"b"})
    # The park's checkpoint already carries a's successor, so a re-park cannot lose it either.
    assert cp["ready_set"] == ["b", "enda"]
    assert ends == ["enda", "endb"]
    assert _trace(events) == [
        "exit:b:completed",
        "enter:enda", "enter:endb", "end:enda=out-a", "exit:enda:completed", "end:endb=out-b", "exit:endb:completed",
    ]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_fanout_in_the_parking_superstep_keeps_its_instances() -> None:
    graph = Graph(
        id="g-fo-park", description="begin -> {fo, b}; fo -> w[0..1]; w -> endw; b -> endb",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="w", count=2)]),
            _ToolCallNode(id="w", tool_id="x__w", arguments={"i": "{{ fanout_index }}"}),
            _tc("b"),
            _EndNode(id="endw", output_template="w"),
            _EndNode(id="endb", output_template="b"),
        ],
        edges=_edges(("begin", "fo"), ("begin", "b"), ("w", "endw"), ("b", "endb")),
    )
    run, cp, events, ends = await _park_then_resume(graph, {"b"})
    assert sorted(cp["fanout_instances"]) == ["w[0]", "w[1]"]
    assert cp["ready_set"] == ["b", "w[0]", "w[1]"]
    assert ends == ["endb", "endw"]
    assert run.outputs()["w"] == [("out-w0", None), ("out-w1", None)]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_completed_sibling_successors_survive_a_partial_resume() -> None:
    graph = Graph(
        id="g-sib3", description="begin -> {a, b, c}; a -> enda; b -> endb; c -> endc",
        nodes=[
            _BeginNode(id="begin"), _tc("a"), _tc("b"), _tc("c"),
            _EndNode(id="enda", output_template="a"), _EndNode(id="endb", output_template="b"), _EndNode(id="endc", output_template="c"),
        ],
        edges=_edges(("begin", "a"), ("begin", "b"), ("begin", "c"), ("a", "enda"), ("b", "endb"), ("c", "endc")),
    )
    run = _Run(graph)
    dispatch = _gate({"b", "c"})
    _events, parked = await run.invoke(dispatch)
    _events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-b", resumed_event_key="tool_approval:sid:b")
    assert isinstance(parked, YieldToWorker)
    assert parked.graph_checkpoint["ready_set"] == ["c", "enda", "endb"]
    events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-c", resumed_event_key="tool_approval:sid:c")
    assert parked is None
    assert sorted(e.end_node_id for e in events if isinstance(e, _GraphEndOutputEvent)) == ["enda", "endb", "endc"]


@pytest.mark.asyncio
async def test_a_routing_failure_of_the_finished_sibling_ends_the_run_failed_after_the_answer() -> None:
    """The finished sibling's edge cannot be routed (it has no parsed output and the router no default): the park still happens, the failure is
    remembered across it, and the run ends ``failed``/``routing_failed`` once the parked node is answered, as a seeded failure does."""
    from primer.model.graph import BranchCondition, JsonPathBranch

    graph = Graph(
        id="g-sib-route", description="begin -> {a, b}; a -?-> enda; b -> endb",
        nodes=[
            _BeginNode(id="begin"), _tc("a"), _tc("b"),
            _EndNode(id="enda", output_template="a"), _EndNode(id="endb", output_template="b"),
        ],
        edges=[
            *_edges(("begin", "a"), ("begin", "b"), ("b", "endb")),
            _ConditionalEdge(
                from_node="a",
                router=_JsonPathRouter(branches=[JsonPathBranch(
                    conditions=[BranchCondition(path="go", op="eq", value="x")], to_node="enda",
                )]),
            ),
        ],
    )
    run = _Run(graph)
    dispatch = _gate({"b"})
    events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    assert _trace(events)[-1] == "error:routing_failed@a"
    cp = parked.graph_checkpoint
    assert (cp["pending_ended_reason"], cp["pending_ended_detail"]) == ("failed", "routing_failed")
    events, parked = await run.resume(dispatch, cp)
    assert parked is None
    assert _trace(events) == ["exit:b:completed"]
    assert await run.ended() == ("ended", "failed", "routing_failed")
