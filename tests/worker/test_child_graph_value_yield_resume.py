"""A value-yielding ``tool_call`` node INSIDE an invoked child graph resumes with the operator's answer.

An agent calls ``system__invoke_graph``; the child graph parks on a value-yielding ``tool_call`` node (``ask_user``, or a
python-toolset tool). The worker resumes it through the continuation walk: ``GraphFrame.resume_leaf`` ->
``resume_invoke_graph`` -> the child executor's ``resume_from_checkpoint``, which runs the tool's resume hook. The hook must
get what the top-level graph resume gives it: the operator's reply (``toolcall_payload``) and the ``ResumeContext`` the
agent-session and agent-node resumes build (the session being resumed, the provider registry's toolset resolver).

Nothing here mocks the helper under test. ``run_invoke_graph`` parks the child and pushes the ``GraphFrame``;
``build_invocation_services`` (the production bundle for the walk) binds the session and registry;
``resume_continuation`` walks the frames; the child is a real ``GraphExecutor`` with only its dispatcher stubbed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from primer.graph.executor import GraphExecutor
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.chat import ToolCallResult
from primer.model.graph import GraphNodeMessage, GraphThread
from primer.model.yield_ import Yielded, YieldToWorker
from primer.toolset.python_runner.provider import python_tool_resume, scoped_tool_name
from primer.worker.continuation import Deliver, resume_continuation
from primer.worker.pool import WorkerPool
from primer.worker.session_resume_coordinator import build_invocation_services
from primer.worker.yield_resume_registry import ResumeContext, register_resume_hook

from tests.graph.test_toolcall_ask_user_value_resume import _build_graph, _make_executor
from tests.graph.test_toolcall_dispatch import _InMemoryStorage
from tests.worker.test_graph_toolcall_value_yield_context import _PythonRegistry, _Registry

_TCID = "tc-child-vy"
_SESSION_ID = "sess-agent"
_REPLY = {"response": "blue"}


class _Pool(SimpleNamespace):
    """The slice of ``WorkerPool`` the invocation services read, with the real agent-node hook seam."""

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload, *, session_id):
        return await WorkerPool._graph_agent_tool_result(self, checkpoint, tcid, payload, session_id=session_id)


async def _park_and_resume(tool_name: str, resume_metadata: dict[str, Any], registry: Any, payload: Any):
    """Park the child graph through ``run_invoke_graph``, then resume it through ``resume_continuation``."""
    graph = _build_graph()

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(
            Yielded(tool_name=tool_name, event_key=f"{tool_name}:s:{_TCID}", resume_metadata=resume_metadata),
            tool_call_id=_TCID,
        )

    async def resume_dispatcher(node, arguments, bypass_approval=False):  # pragma: no cover - must not re-dispatch
        raise AssertionError("a value-yielding tool_call must not be re-dispatched")

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=thread_storage)  # type: ignore[arg-type]
    executors = [
        _make_executor(graph, thread, thread_storage, message_storage, first_dispatcher),
        _make_executor(graph, thread, thread_storage, message_storage, resume_dispatcher),
    ]

    async def resolve_graph(graph_id: str):
        return graph

    async def build_child_executor(*, graph, gsid):
        return executors.pop(0)

    graph_services = GraphInvocationServices(
        resolve_graph=resolve_graph, build_child_executor=build_child_executor,
        session_id=_SESSION_ID, workspace_id="ws-1", graph_session_id="gs-agent",
    )
    with pytest.raises(YieldToWorker) as parked:
        await run_invoke_graph(graph_id="child", graph_input="go", services=graph_services, tool_call_id="agent-tc")

    pool = _Pool(_provider_registry=registry, _storage=None, _approval_resolver=None)
    services = build_invocation_services(
        pool, SimpleNamespace(id=_SESSION_ID), None, None, SimpleNamespace(_graph_services=graph_services),
    )
    return await resume_continuation(parked.value.frames, parked.value.yielded, payload, services)


@pytest.mark.asyncio
async def test_the_hook_in_an_invoked_child_graph_gets_the_reply_and_the_context():
    seen: list[tuple[Any, ResumeContext]] = []

    def hook(meta, payload, ctx: ResumeContext) -> ToolCallResult:
        seen.append((payload, ctx))
        return ToolCallResult(output=json.dumps(payload), is_error=False)

    register_resume_hook("test_child_vy_ctx", hook)

    outcome = await _park_and_resume("test_child_vy_ctx", {"q": "?"}, _Registry(), _REPLY)

    assert isinstance(outcome, Deliver)
    ((payload, ctx),) = seen
    assert payload == _REPLY, "the operator's answer never reached the hook"
    assert (ctx.tool_name, ctx.tool_call_id) == ("test_child_vy_ctx", _TCID)
    assert ctx.session_id == _SESSION_ID, "the hook was not told which session it is answering"
    assert ctx.resolve_provider is not None, "a python toolset's hook could not reach its provider"
    assert await ctx.resolve_provider("ts-any") == "ts-any", "the resolver does not reach the registry"
    assert outcome.tool_result.id == "agent-tc"
    assert json.loads(json.loads(outcome.tool_result.output)["output"]) == _REPLY


@pytest.mark.asyncio
async def test_a_python_toolset_tool_yielding_in_an_invoked_child_graph_resumes():
    name = scoped_tool_name("ts-vy", "ask")
    register_resume_hook(name, python_tool_resume)

    outcome = await _park_and_resume(name, {"toolset_id": "ts-vy", "tool_id": "ask"}, _PythonRegistry(), _REPLY)

    assert isinstance(outcome, Deliver)
    assert json.loads(json.loads(outcome.tool_result.output)["output"]) == {"tool_id": "ask", "answer": "blue"}
