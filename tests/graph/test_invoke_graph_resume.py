"""invoke_graph HITL parking + resume (Task 5 / 5.1).

Stub child executors (no live graph): exercise the descent of a child-graph
park into a ``GraphFrame`` pushed onto the re-raised child yield (the worker
then routes it through the continuation walk), and the resume drain that
collects the graph's terminal output (or catches a re-park).
"""

import pytest

from primer.graph.invoke_graph import (
    GraphInvocationServices,
    resume_invoke_graph,
    run_invoke_graph,
)
from primer.model.yield_ import Yielded, YieldToWorker


class _ParkingExec:
    """Child executor whose .invoke parks immediately (HITL gate)."""

    async def invoke(self, messages):
        y = YieldToWorker(
            Yielded(
                tool_name="_approval",
                event_key="ask:1",
                resume_metadata={"x": 1},
            ),
            tool_call_id="child-tc",
        )
        y.graph_checkpoint = {"snap": "shot"}
        raise y
        yield  # unreachable; makes this an async generator


def _svc(build):
    async def _resolve(gid):
        # The stub build_child_executor ignores the graph object, so a bare
        # sentinel is enough - no need to satisfy the full Graph schema.
        return {"id": gid}

    return GraphInvocationServices(
        resolve_graph=_resolve,
        build_child_executor=build,
        session_id="s",
        workspace_id="w",
        graph_session_id="gs",
    )


@pytest.mark.asyncio
async def test_run_invoke_graph_pushes_graph_frame():
    async def _build(*, graph, gsid):
        return _ParkingExec()

    svc = _svc(_build)
    with pytest.raises(YieldToWorker) as ei:
        await run_invoke_graph(
            graph_id="g", graph_input="i", services=svc, tool_call_id="atc"
        )
    y = ei.value
    # The descent re-raises the CHILD graph's REAL leaf (its own gate), NOT a
    # re-stamped invoke_graph park.
    assert y.yielded.tool_name == "_approval"
    assert y.yielded.event_key == "ask:1"
    assert y.tool_call_id == "child-tc"
    assert y.graph_checkpoint == {"snap": "shot"}
    # A single GraphFrame is prepended onto the yield's frame stack.
    from primer.worker.frames import GraphFrame

    assert len(y.frames) == 1
    gf = y.frames[0]
    assert isinstance(gf, GraphFrame)
    assert gf.graph_id == "g"
    assert gf.gsid == "gs__invoke_atc"
    assert gf.checkpoint == {"snap": "shot"}
    # tool_call_id = the AGENT's invoke_graph call id (caller-result id).
    assert gf.tool_call_id == "atc"
    # node_tcid = the CHILD graph's parked-node id (resumed_tcid).
    assert gf.node_tcid == "child-tc"


class _CompletingExec:
    """Child executor whose resume drains to a terminal End output."""

    async def resume_from_checkpoint(self, checkpoint, *, resumed_tcid=None,
                                     agent_tool_result=None):
        from primer.graph.base import _GraphEndOutputEvent

        yield _GraphEndOutputEvent(text="done!", parsed=None, end_node_id="end")


@pytest.mark.asyncio
async def test_resume_invoke_graph_drains_to_completion():
    out, repark = await resume_invoke_graph(
        child=_CompletingExec(),
        checkpoint={"snap": "shot"},
        payload={"decision": "approved"},
        resumed_tcid="child-tc",
        agent_tool_result=None,
    )
    assert out == "done!"
    assert repark is None


class _ReparkingExec:
    """Child executor whose resume parks again (another gate pending)."""

    def __init__(self):
        self.repark = YieldToWorker(
            Yielded(tool_name="_approval", event_key="ask:2"),
            tool_call_id="child-tc-2",
        )
        self.repark.graph_checkpoint = {"snap": "shot2"}

    async def resume_from_checkpoint(self, checkpoint, *, resumed_tcid=None,
                                     agent_tool_result=None):
        raise self.repark
        yield  # unreachable; async generator


@pytest.mark.asyncio
async def test_resume_invoke_graph_reparks():
    child = _ReparkingExec()
    out, repark = await resume_invoke_graph(
        child=child,
        checkpoint={"snap": "shot"},
        payload={"decision": "approved"},
        resumed_tcid="child-tc",
        agent_tool_result=None,
    )
    assert not out  # None or empty
    assert repark is child.repark
