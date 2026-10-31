"""AG-H3: the one-shot ``on_max_iterations`` reroute survives a park.

``_max_iter_routed`` was not in the checkpoint, so a landing node that parked
re-tripped the cap on every resume: it was dispatched (and asked the person)
again each time and its End successor never ran. Inverted from the review
probe ``graph/test_probe_cap_resume.py``.
"""

from __future__ import annotations

import pytest

from primer.graph.base import _GraphEndOutputEvent
from primer.model.chat import ToolResultPart
from primer.model.yield_ import Yielded, YieldToWorker

from tests.graph.test_graph_resume_characterization import _loop_graph, _Run, _trace


@pytest.mark.asyncio
async def test_rerouted_landing_node_that_parks_is_dispatched_once() -> None:
    run = _Run(_loop_graph("f"))
    f_dispatches = {"n": 0}

    async def dispatch(node, arguments, bypass_approval=False):
        if node.id == "f" and not bypass_approval:
            f_dispatches["n"] += 1
            raise YieldToWorker(Yielded(tool_name="_approval", event_key="tool_approval:sid:f"), tool_call_id="tc-f")
        return ToolResultPart(id="x", output=f"out-{node.id}")

    _events, parked = await run.invoke(dispatch)
    assert isinstance(parked, YieldToWorker)
    assert parked.graph_checkpoint["max_iter_routed"] is True
    events, again = await run.resume(dispatch, parked.graph_checkpoint, resumed_tcid="tc-f")
    assert again is None
    assert f_dispatches["n"] == 1
    assert [e.end_node_id for e in events if isinstance(e, _GraphEndOutputEvent)] == ["end"]
    assert _trace(events) == ["exit:f:completed", "enter:end", "end:end=out-f", "exit:end:completed"]
    assert await run.ended() == ("ended", "completed", None)


@pytest.mark.asyncio
async def test_the_cap_flag_round_trips_through_the_checkpoint() -> None:
    run = _Run(_loop_graph("f"))
    ex = await run._executor(None)
    ex._max_iter_routed = True
    cp = ex.snapshot_state()
    assert cp["max_iter_routed"] is True
    ex2 = await run._executor(None)
    ex2.restore_state(cp)
    assert ex2._max_iter_routed is True
