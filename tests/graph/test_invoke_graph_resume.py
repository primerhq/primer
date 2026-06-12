"""invoke_graph HITL parking + resume (Task 5).

Stub child executors (no live graph): exercise the re-stamp of a child-graph
park as an ``invoke_graph`` park on the AGENT session, and the resume drain
that collects the graph's terminal output (or catches a re-park).
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
async def test_run_invoke_graph_reparks_as_invoke_graph():
    async def _build(*, graph, gsid):
        return _ParkingExec()

    svc = _svc(_build)
    with pytest.raises(YieldToWorker) as ei:
        await run_invoke_graph(
            graph_id="g", graph_input="i", services=svc, tool_call_id="atc"
        )
    y = ei.value
    assert y.yielded.tool_name == "invoke_graph"
    assert y.yielded.resume_metadata["invoke_graph"] is True
    assert y.yielded.resume_metadata["sub_gsid"] == "gs__invoke_atc"
    assert y.yielded.resume_metadata["graph_id"] == "g"
    assert y.yielded.resume_metadata["child_tcid"] == "child-tc"
    assert y.yielded.event_key == "ask:1"
    assert y.graph_checkpoint == {"snap": "shot"}
    assert y.tool_call_id == "atc"


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
