import json
import pytest
from primer.worker.frames import apply_leaf, AgentFrame, AgentResumeContext, Reparked
from primer.model.chat import ToolResultPart, ToolCallPart
from primer.model.yield_ import Yielded, YieldToWorker
from types import SimpleNamespace
from primer.model.chat import ToolCallResult
from primer.toolset.python_runner.provider import python_tool_resume, scoped_tool_name
from primer.worker.session_resume_coordinator import build_invocation_services
from primer.worker.yield_resume_registry import ResumeContext, register_resume_hook
from tests.worker.test_child_graph_value_yield_resume import _Pool
from tests.worker.test_graph_toolcall_value_yield_context import _PythonRegistry, _Registry

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
    session_id = "ses"
    resolve_provider = None
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
    def _fake_hook(meta, payload, ctx): return _HookResult()
    import primer.worker.frames as frames_mod
    monkeypatch.setattr(frames_mod, "get_resume_hook", lambda name: _fake_hook, raising=False)
    leaf = Yielded(tool_name="ask_user", event_key="ask_user:ses:c1", resume_metadata={})
    out = await apply_leaf(_agent_frame(), leaf, {"text": "hi"}, _Services(_TM()))
    assert out.error is False and out.output == '{"answer": 42}' and out.id == "c1"


def _real_services(registry):
    """The production InvocationServices bundle for session ``ses``: what the worker hands the walk."""
    pool = _Pool(_provider_registry=registry, _storage=None, _approval_resolver=None)
    return build_invocation_services(pool, SimpleNamespace(id="ses"), None, None, SimpleNamespace())


@pytest.mark.asyncio
async def test_apply_leaf_hook_in_a_nested_subagent_gets_the_session_and_the_registry():
    """A yielding tool inside a NESTED SUBAGENT resumes through ``apply_leaf``; its hook gets a real ResumeContext."""
    seen = []

    def hook(meta, payload, ctx: ResumeContext):
        seen.append((payload, ctx))
        return ToolCallResult(output='{"ok": true}', is_error=False)

    register_resume_hook("test_apply_leaf_ctx", hook)
    leaf = Yielded(tool_name="test_apply_leaf_ctx", event_key="test_apply_leaf_ctx:ses:c1", resume_metadata={})

    out = await apply_leaf(_agent_frame(), leaf, {"response": "blue"}, _real_services(_Registry()))

    assert out.error is False and out.id == "c1"
    ((payload, ctx),) = seen
    assert payload == {"response": "blue"}
    assert (ctx.tool_name, ctx.tool_call_id) == ("test_apply_leaf_ctx", "c1")
    assert ctx.session_id == "ses", "the hook was not told which session it is answering"
    assert ctx.resolve_provider is not None, "a python toolset's hook could not reach its provider"
    assert await ctx.resolve_provider("ts-any") == "ts-any", "the resolver does not reach the registry"


@pytest.mark.asyncio
async def test_apply_leaf_python_toolset_tool_in_a_nested_subagent_resumes():
    name = scoped_tool_name("ts-vy", "ask")
    register_resume_hook(name, python_tool_resume)
    leaf = Yielded(tool_name=name, event_key=f"{name}:ses:c1", resume_metadata={"toolset_id": "ts-vy", "tool_id": "ask"})

    out = await apply_leaf(_agent_frame(), leaf, {"response": "blue"}, _real_services(_PythonRegistry()))

    assert out.error is False, out.output
    assert json.loads(out.output) == {"tool_id": "ask", "answer": "blue"}
