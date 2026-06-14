"""invoke_graph descent into a GraphFrame (Task 5.1).

When the child graph hits a HITL gate, ``run_invoke_graph`` no longer
re-stamps the park as a synthetic ``invoke_graph`` park. Instead it pushes a
:class:`~primer.worker.frames.GraphFrame` (carrying the child's identity +
checkpoint) onto the child yield's ``frames`` stack and re-raises the child's
REAL leaf (its own ``_approval`` / ``ask_user`` / ... gate). The worker then
routes that park through the generic continuation walk.

The frame carries TWO distinct ids:

* ``tool_call_id`` - the AGENT's invoke_graph call id (the caller-result id),
* ``node_tcid`` - the CHILD graph's parked-node tcid (the resumed_tcid).
"""

import pytest

from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.frames import GraphFrame


class _ParkingExec:
    """Child executor whose .invoke parks immediately on its own gate."""

    async def invoke(self, messages):
        y = YieldToWorker(
            Yielded(
                tool_name="ask_user",
                event_key="ask_user:gs:child-node",
                resume_metadata={"prompt": "hi?"},
            ),
            tool_call_id="child-node",
        )
        y.graph_checkpoint = {"snap": "shot"}
        raise y
        yield  # unreachable; makes this an async generator


def _svc(build):
    async def _resolve(gid):
        return {"id": gid}

    return GraphInvocationServices(
        resolve_graph=_resolve,
        build_child_executor=build,
        session_id="s",
        workspace_id="w",
        graph_session_id="gs",
    )


@pytest.mark.asyncio
async def test_descent_pushes_graph_frame_and_reraises_child_leaf():
    async def _build(*, graph, gsid):
        return _ParkingExec()

    svc = _svc(_build)
    with pytest.raises(YieldToWorker) as ei:
        await run_invoke_graph(
            graph_id="g", graph_input="i", services=svc, tool_call_id="agent-tc"
        )
    y = ei.value

    # The re-raised yield IS the child graph's real leaf, not a re-stamp.
    assert y.yielded.tool_name == "ask_user"
    assert y.yielded.tool_name != "invoke_graph"
    assert y.yielded.event_key == "ask_user:gs:child-node"
    assert y.tool_call_id == "child-node"
    assert y.graph_checkpoint == {"snap": "shot"}

    # Exactly one GraphFrame, prepended.
    assert len(y.frames) == 1
    gf = y.frames[0]
    assert isinstance(gf, GraphFrame)
    assert gf.graph_id == "g"
    assert gf.gsid == "gs__invoke_agent-tc"
    assert gf.checkpoint == {"snap": "shot"}
    # The two distinct ids.
    assert gf.tool_call_id == "agent-tc"   # AGENT's invoke_graph call id
    assert gf.node_tcid == "child-node"    # CHILD graph's parked-node id


@pytest.mark.asyncio
async def test_descent_prepends_onto_existing_child_frames():
    """A child yield that already carries frames (a deeper nested invocation)
    gets the GraphFrame prepended, preserving root-first order."""
    existing = object()

    class _NestedParkingExec:
        async def invoke(self, messages):
            y = YieldToWorker(
                Yielded(tool_name="_approval", event_key="ap:gs:n"),
                tool_call_id="n",
            )
            y.graph_checkpoint = {"snap": 1}
            y.frames = [existing]
            raise y
            yield

    async def _build(*, graph, gsid):
        return _NestedParkingExec()

    svc = _svc(_build)
    with pytest.raises(YieldToWorker) as ei:
        await run_invoke_graph(
            graph_id="g", graph_input="i", services=svc, tool_call_id="agent-tc"
        )
    y = ei.value
    assert len(y.frames) == 2
    assert isinstance(y.frames[0], GraphFrame)  # prepended (root-first)
    assert y.frames[1] is existing
