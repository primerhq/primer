"""The outer approval yield carries the full event_keys set (tool_call + agent)."""
import pytest

from tests.graph.test_agent_node_yield_capture import (
    _YieldingLLM, _drain_until_yield, _mk_executor, _graph,
)


@pytest.mark.asyncio
async def test_outer_yield_carries_event_keys_for_agent_yield():
    ex = await _mk_executor(_graph(), _YieldingLLM())
    _evs, raised = await _drain_until_yield(ex.invoke([]))
    assert raised is not None
    assert raised.yielded.event_keys == ["ask_user:t1:tc1"]
    assert raised.graph_checkpoint is not None
    # checkpoint round-trips the pending agent yield
    assert raised.graph_checkpoint["pending_agent_yields"][0]["event_key"] == "ask_user:t1:tc1"
