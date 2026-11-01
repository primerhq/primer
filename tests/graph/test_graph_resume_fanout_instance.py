"""AG-H2: a resumed fan-out instance reaches the aggregator, the drain count and its spec's ``on_failure`` policy.

The resume arms used to write only ``context.nodes[<instance>]`` on success and
fail the whole run on any failure, so a FanIn after a parked instance never
fired (the run ended ``completed`` with no End output), ``drain_then_fail``
never drained and ``collect`` failed the graph. Inverted from the review probe
``graph/test_probe_fanout_resume.py``, plus the ``drain_then_fail``, ``collect``
and agent-node (tool_wait) variants.
"""

from __future__ import annotations

import pytest

from primer.graph._node_identity import current_graph_node_id
from primer.graph.base import _ToolApprovalRejected
from primer.model.chat import ToolResultPart
from primer.model.graph import (
    FanOutSpec,
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
)
from primer.model.yield_ import ToolWaitPark, YieldToWorker

from tests.graph.test_graph_resume_characterization import _drive, _edges, _fanout_graph, _gate, _Run, _trace
from tests.graph.test_tool_wait_graph_park import _ask_user_yield, _mk_executor_for_graph, _tool_wait_park


@pytest.mark.asyncio
async def test_resumed_instance_reaches_the_fanin() -> None:
    run = _Run(_fanout_graph())
    dispatch = _gate({"t1"})
    _events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-t1")
    assert parked is None
    assert _trace(events) == [
        "exit:t[1]:completed",
        "enter:fi", "exit:fi:completed",
        "enter:end", "end:end=out-t0,out-t1", "exit:end:completed",
    ]
    assert run.outputs()["t"] == [("out-t0", None), ("out-t1", None)]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_two_resumed_instances_reach_the_fanin_one_answer_at_a_time() -> None:
    run = _Run(_fanout_graph())
    dispatch = _gate({"t0", "t1"})
    _events, parked = await run.invoke(dispatch)
    _events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-t0", resumed_event_key="tool_approval:sid:t0")
    assert isinstance(parked, YieldToWorker)
    events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-t1", resumed_event_key="tool_approval:sid:t1")
    assert parked is None
    assert "end:end=out-t0,out-t1" in _trace(events)
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_drain_then_fail_counts_the_resumed_instance() -> None:
    """t[0] fails (suppressed) while t[1] parks; once t[1] is answered every instance has reported and the run fails as the main loop fails it."""
    run = _Run(_fanout_graph("drain_then_fail"))

    async def dispatch(node, arguments, bypass_approval=False):
        if arguments["i"] == "0":
            raise RuntimeError("boom-t0")
        return await _gate({"t1"})(node, arguments, bypass_approval)

    _events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    events, parked = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-t1")
    assert parked is None
    assert _trace(events) == ["exit:t[1]:completed", "error:fanin_upstream_failed@t[0]"]
    assert await run.ended() == ("ended", "failed", "fanin_upstream_failed")


@pytest.mark.asyncio
async def test_drain_then_fail_suppresses_a_rejected_resumed_instance() -> None:
    run = _Run(_fanout_graph("drain_then_fail"))
    _events, parked = await run.invoke(_gate({"t1"}))

    async def rejecting(node, arguments, bypass_approval=False):
        raise _ToolApprovalRejected("no", tool_call_id="tc-t1", kind="rejected")

    events, parked = await run.resume(rejecting, parked.graph_checkpoint, resumed_tcid="tc-t1")
    assert parked is None
    assert _trace(events) == ["exit:t[1]:failed", "error:fanin_upstream_failed@t[1]"]
    assert await run.ended() == ("ended", "failed", "fanin_upstream_failed")


@pytest.mark.asyncio
async def test_collect_keeps_a_rejected_resumed_instance_in_the_aggregator() -> None:
    run = _Run(_fanout_graph("collect"))
    _events, parked = await run.invoke(_gate({"t1"}))

    async def rejecting(node, arguments, bypass_approval=False):
        raise _ToolApprovalRejected("no", tool_call_id="tc-t1", kind="rejected")

    events, parked = await run.resume(rejecting, parked.graph_checkpoint, resumed_tcid="tc-t1")
    assert parked is None
    assert _trace(events) == [
        "exit:t[1]:failed",
        "enter:fi", "exit:fi:completed",
        "enter:end", "end:end=out-t0,", "exit:end:completed",
    ]
    assert run.outputs()["t"] == [("out-t0", None), ("", "no")]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_fail_fast_still_fails_the_run_on_a_rejected_resumed_instance() -> None:
    run = _Run(_fanout_graph())
    _events, parked = await run.invoke(_gate({"t1"}))

    async def rejecting(node, arguments, bypass_approval=False):
        raise _ToolApprovalRejected("no", tool_call_id="tc-t1", kind="rejected")

    events, parked = await run.resume(rejecting, parked.graph_checkpoint, resumed_tcid="tc-t1")
    assert parked is None
    assert _trace(events) == ["error:tool_approval_rejected@t[1]", "exit:t[1]:failed"]
    assert await run.ended() == ("ended", "failed", "tool_approval_rejected")


def _agent_fanout_graph() -> Graph:
    return Graph(
        id="g-fo-agent", description="begin -> fo -> w[0..1] (agent) -> fi -> end",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="w", count=2)]),
            _AgentNodeRef(id="w", agent_id="agent-w"),
            _FanInNode(id="fi", aggregate_template="{{ nodes.w | length }}"),
            _EndNode(id="end", output_template="{{ nodes.fi.text }}"),
        ],
        edges=_edges(("begin", "fo"), ("w", "fi"), ("fi", "end")),
    )


def _park_instance_once(monkeypatch, instance: str, park) -> None:
    import primer.graph._agent_node as agent_node_mod

    parked: set[str] = set()

    async def _spy(**kwargs):
        if current_graph_node_id() == instance and instance not in parked:
            parked.add(instance)
            raise park
            yield  # pragma: no cover - keeps this a generator
        return
        yield  # pragma: no cover - keeps this a generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)


@pytest.mark.asyncio
async def test_resumed_tool_wait_instance_reaches_the_fanin(monkeypatch) -> None:
    ex = await _mk_executor_for_graph(_agent_fanout_graph())
    _park_instance_once(monkeypatch, "w[1]", _tool_wait_park("w[1]", "1"))
    _events, parked = await _drive(ex.invoke([]))
    assert isinstance(parked, ToolWaitPark)
    events, parked = await _drive(ex.resume_from_checkpoint(
        parked.graph_checkpoint, resolved_tool_wait={"w[1]": [ToolResultPart(id="w[1]:tool:0:1", output="r")]},
    ))
    assert parked is None
    assert "end:end=2" in _trace(events)
    assert ex._last_ended_reason == "completed"


@pytest.mark.asyncio
async def test_resumed_agent_yield_instance_reaches_the_fanin(monkeypatch) -> None:
    from primer.model.chat import Message

    ex = await _mk_executor_for_graph(_agent_fanout_graph())
    _park_instance_once(monkeypatch, "w[1]", _ask_user_yield("w[1]", "tc-w"))
    _events, parked = await _drive(ex.invoke([]))
    assert isinstance(parked, YieldToWorker)
    events, parked = await _drive(ex.resume_from_checkpoint(
        parked.graph_checkpoint, resumed_tcid="tc-w",
        agent_tool_result=Message(role="tool", parts=[ToolResultPart(id="tc-w", output="blue")]),
    ))
    assert parked is None
    assert "end:end=2" in _trace(events)
    assert ex._last_ended_reason == "completed"
