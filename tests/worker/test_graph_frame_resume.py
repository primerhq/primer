import json, pytest
import primer.worker.frames as frames_mod
from primer.worker.frames import GraphFrame, Completed, Reparked
from primer.model.chat import ToolResultPart
from primer.model.yield_ import Yielded, YieldToWorker

class _Services:
    async def resolve_graph(self, graph_id): return {"id": graph_id}
    async def build_child_graph_executor(self, graph, gsid): return object()

def _frame():
    # tool_call_id = the AGENT's invoke_graph call id (caller-result id);
    # node_tcid = the CHILD graph's parked-node id (resumed_tcid). Distinct.
    return GraphFrame(
        graph_id="g1", gsid="gs1", checkpoint={"k": 1},
        tool_call_id="agent-tc", node_tcid="node-tc",
    )

@pytest.mark.asyncio
async def test_graph_frame_resume_completes(monkeypatch):
    async def _fake_resume(*, child, checkpoint, payload, resumed_tcid=None, agent_tool_result=None):
        assert checkpoint == {"k": 1}
        assert resumed_tcid == "node-tc"  # the child node's tcid, not the agent's
        assert agent_tool_result is not None  # child_result was wrapped + delivered
        return ("graph output", None)
    monkeypatch.setattr(frames_mod, "resume_invoke_graph", _fake_resume, raising=False)
    out = await _frame().resume(ToolResultPart(id="x", output="child", error=False), _Services())
    assert isinstance(out, Completed)
    assert out.value.id == "agent-tc"  # result pairs with the agent's call id
    assert json.loads(out.value.output)["output"] == "graph output"

@pytest.mark.asyncio
async def test_graph_frame_resume_reparks(monkeypatch):
    repark = YieldToWorker(Yielded(tool_name="ask_user", event_key="ask_user:gs1:n2"), tool_call_id="n2")
    async def _fake_resume(**kw): return (None, repark)
    monkeypatch.setattr(frames_mod, "resume_invoke_graph", _fake_resume, raising=False)
    out = await _frame().resume(ToolResultPart(id="x", output="child", error=False), _Services())
    assert isinstance(out, Reparked) and out.new_yield is repark
