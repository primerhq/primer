import json
import pytest
from primer.worker.frames import AgentFrame, AgentResumeContext, Completed, Reparked
from primer.model.chat import ToolResultPart
from primer.model.yield_ import Yielded, YieldToWorker

def _frame():
    return AgentFrame(agent_id="a1", llm_messages=[{"role": "assistant", "parts": []}],
                      tool_call_id="inv-tc", depth=2,
                      context=AgentResumeContext("ses", "ws", None, "u", ["system__ask_user"]))

class _ServicesCompletes:
    def __init__(self): self.captured = None
    async def resume_subagent(self, **kw):
        self.captured = kw
        return "subagent final answer"

class _ServicesReyields:
    async def resume_subagent(self, **kw):
        raise YieldToWorker(Yielded(tool_name="ask_user", event_key="ask_user:ses:n2"), tool_call_id="n2")

class _ServicesStreamFails:
    async def resume_subagent(self, **kw):
        from primer.model.chat import Error, TurnStreamFailure
        raise TurnStreamFailure(
            Error(code="llm_connect_error", message="connect failed", fatal=True),
            partial_messages=[], rounds_completed=0,
        )

@pytest.mark.asyncio
async def test_agent_frame_resume_completes_returns_text_toolresult():
    svc = _ServicesCompletes()
    out = await _frame().resume(ToolResultPart(id="x", output="child", error=False), svc)
    assert isinstance(out, Completed)
    assert out.value.id == "inv-tc"
    assert json.loads(out.value.output)["output"] == "subagent final answer"
    # frame args were forwarded
    assert svc.captured["agent_id"] == "a1"
    assert svc.captured["invoke_tool_call_id"] == "inv-tc"
    assert svc.captured["depth"] == 2

@pytest.mark.asyncio
async def test_agent_frame_resume_reyields_reparks():
    out = await _frame().resume(ToolResultPart(id="x", output="child", error=False), _ServicesReyields())
    assert isinstance(out, Reparked) and out.new_yield.yielded.tool_name == "ask_user"

@pytest.mark.asyncio
async def test_agent_frame_resume_turn_stream_failure_completes_as_error():
    """01a070d6: a subagent CONTINUATION whose LLM connection failed must
    not resolve this frame as a SUCCESSFUL empty-text tool result - the
    parent turn needs to see the failure, not silently believe the
    subagent finished with nothing to say."""
    out = await _frame().resume(
        ToolResultPart(id="x", output="child", error=False), _ServicesStreamFails(),
    )
    assert isinstance(out, Completed)
    assert out.value.id == "inv-tc"
    assert out.value.error is True
    assert "connect failed" in json.loads(out.value.output)["error"]
