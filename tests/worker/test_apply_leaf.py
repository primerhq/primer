import json, pytest
from primer.worker.frames import apply_leaf, AgentFrame, AgentResumeContext, Reparked
from primer.model.chat import ToolResultPart, ToolCallPart
from primer.model.yield_ import Yielded, YieldToWorker

def _leaf_approval(call_id="c1"):
    return Yielded(tool_name="_approval", event_key=f"tool_approval:ses:{call_id}",
        resume_metadata={"original_call": {"id": call_id, "name": "system__delete_agent", "arguments": {"id": "x"}}})

def _agent_frame(call_id="c1", tools=None):
    return AgentFrame(agent_id="a", llm_messages=[], tool_call_id=call_id, depth=1,
                      context=AgentResumeContext("ses", "ws", None, "u", tools or []))

class _TM:
    def __init__(self, result=None, raises=None): self._r, self._raise = result, raises
    async def execute(self, call, *, bypass_approval):
        if self._raise: raise self._raise
        return self._r

class _Services:
    def __init__(self, tm): self._tm = tm
    async def build_subagent_toolmanager(self, ctx): return self._tm

@pytest.mark.asyncio
async def test_apply_leaf_approval_approved_dispatches():
    tm = _TM(result=ToolResultPart(id="c1", output="ok", error=False))
    out = await apply_leaf(_agent_frame(), _leaf_approval(), {"decision": "approved"}, _Services(tm))
    assert isinstance(out, ToolResultPart) and out.output == "ok" and out.error is False

@pytest.mark.asyncio
async def test_apply_leaf_approval_rejected_is_error():
    out = await apply_leaf(_agent_frame(), _leaf_approval(), {"decision": "rejected", "reason": "no"}, _Services(_TM()))
    assert out.error is True and json.loads(out.output)["rejected"] is True

@pytest.mark.asyncio
async def test_apply_leaf_approved_tool_yields_reparks():
    yld = YieldToWorker(Yielded(tool_name="sleep", event_key="timer:c1"), tool_call_id="c1")
    out = await apply_leaf(_agent_frame(), _leaf_approval(), {"decision": "approved"}, _Services(_TM(raises=yld)))
    assert isinstance(out, Reparked) and out.new_yield.yielded.tool_name == "sleep"

@pytest.mark.asyncio
async def test_apply_leaf_yielding_tool_uses_hook(monkeypatch):
    # patch get_resume_hook so the leaf resolves via the hook path
    class _HookResult:
        output = '{"answer": 42}'
        is_error = False
    def _fake_hook(meta, payload): return _HookResult()
    import primer.worker.frames as frames_mod
    monkeypatch.setattr(frames_mod, "get_resume_hook", lambda name: _fake_hook, raising=False)
    leaf = Yielded(tool_name="ask_user", event_key="ask_user:ses:c1", resume_metadata={})
    out = await apply_leaf(_agent_frame(), leaf, {"text": "hi"}, _Services(_TM()))
    assert out.error is False and out.output == '{"answer": 42}' and out.id == "c1"
