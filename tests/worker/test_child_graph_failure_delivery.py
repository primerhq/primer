"""A child graph that FAILED is an error to the agent that invoked it, not an empty success.

An agent calls ``system__invoke_graph``. When the child graph ends ``failed`` (an operator refused its approval gate, the
gate timed out or was cancelled, the reply was unreadable, a tool crashed, a conditional edge matched nothing) the
executor yields a terminal ``_GraphErrorEvent``. ``run_invoke_graph`` and ``resume_invoke_graph`` only collected the
graph's output text and the end-output event, so the error event was dropped: the invoking agent received
``{"output": ""}`` with ``error=False``, a success-shaped empty result for every one of those failures.

What the agent must now see:

* an approval REFUSAL (rejected, malformed reply, timeout, cancel) in the shape the flat agent-session approval gate
  delivers (``yield_runtime._resume_tool_approval``): ``{"rejected": true, "reason", "tool_name"}``, ``error=True``, so a
  model reads the same text for the same refusal wherever the gate sits;
* any other child failure: ``{"error": <code>, "message", "node_id"}``, ``error=True``;
* an approved gate that completes: unchanged (``{"output": <text>}``, ``error=False``).

Driven through the real ``run_invoke_graph`` (parks the child, pushes the ``GraphFrame``), the production invocation
services bundle and ``resume_continuation`` (the walk that reaches ``GraphFrame.resume_leaf``) over a real
``GraphExecutor`` with only its tool dispatcher stubbed. The first-run failure goes through the real tool handler.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from primer.graph.executor import GraphExecutor
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.chat import ToolCallResult, ToolResultPart
from primer.model.graph import (
    BranchCondition,
    Graph,
    GraphNodeMessage,
    GraphThread,
    JsonPathBranch,
    _BeginNode,
    _ConditionalEdge,
    _EndNode,
    _JsonPathRouter,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.yield_ import Yielded, YieldToWorker
from primer.toolset.workspaces import _invoke_graph_handler
from primer.worker.continuation import Deliver, resume_continuation
from primer.worker.session_resume_coordinator import build_invocation_services

from tests._resume_hook_fakes import (
    AgentNodeHookPool as _Pool,
    IdentityToolsetRegistry as _Registry,
    build_ask_user_graph,
    make_toolcall_executor as _make_executor,
)
from tests.graph.test_toolcall_dispatch import _InMemoryStorage

_TCID = "tc-child-gate"
_SESSION_ID = "sess-agent"
_AGENT_TC = "agent-tc"
_GATED_TOOL = "danger__wipe"


def _routing_graph() -> Graph:
    """begin -> tool(ask) -> a conditional edge that matches nothing and has no default -> end."""
    return Graph(
        id="g-route-miss",
        description="a conditional edge no branch of which matches",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="ask", tool_id="danger__wipe", arguments={}, output_schema={"type": "object"}),
            _EndNode(id="exit", output_template="{{ nodes.ask.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="ask"),
            _ConditionalEdge(
                from_node="ask",
                router=_JsonPathRouter(
                    branches=[JsonPathBranch(conditions=[BranchCondition(path="go", op="eq", value="win")], to_node="exit")],
                ),
            ),
        ],
    )


async def _executors(graph: Graph, *dispatchers: Any):
    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=thread_storage)  # type: ignore[arg-type]
    return [_make_executor(graph, thread, thread_storage, message_storage, d) for d in dispatchers]


def _services(graph: Graph, executors: list[Any]) -> GraphInvocationServices:
    async def resolve_graph(graph_id: str):
        return graph

    async def build_child_executor(*, graph, gsid):
        return executors.pop(0)

    return GraphInvocationServices(
        resolve_graph=resolve_graph, build_child_executor=build_child_executor,
        session_id=_SESSION_ID, workspace_id="ws-1", graph_session_id="gs-agent",
    )


async def _first_run_parks_on_approval(*, graph: Graph | None = None, resumed: Any):
    """Park the child graph on an ``_approval`` gate (through ``run_invoke_graph``) and return what resumes it."""
    graph = graph or build_ask_user_graph()

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(
            Yielded(
                tool_name="_approval", event_key=f"_approval:s:{_TCID}",
                resume_metadata={"original_call": {"id": _TCID, "name": _GATED_TOOL, "arguments": {}}},
            ),
            tool_call_id=_TCID,
        )

    executors = await _executors(graph, first_dispatcher, resumed)
    graph_services = _services(graph, executors)
    with pytest.raises(YieldToWorker) as parked:
        await run_invoke_graph(graph_id="child", graph_input="go", services=graph_services, tool_call_id=_AGENT_TC)

    pool = _Pool(_provider_registry=_Registry(), _storage=None, _approval_resolver=None)
    services = build_invocation_services(
        pool, SimpleNamespace(id=_SESSION_ID), None, None, SimpleNamespace(_graph_services=graph_services),
    )
    return parked.value, services


async def _resume_with(payload: Any, *, resumed: Any, graph: Graph | None = None):
    parked, services = await _first_run_parks_on_approval(graph=graph, resumed=resumed)
    outcome = await resume_continuation(parked.frames, parked.yielded, payload, services)
    assert isinstance(outcome, Deliver), f"the resume did not deliver a result: {outcome!r}"
    return outcome.tool_result


async def _never_re_dispatched(node, arguments, bypass_approval=False):  # pragma: no cover - must not run
    raise AssertionError("a refused gate must not re-dispatch the tool")


async def _tool_runs(node, arguments, bypass_approval=False):
    return ToolResultPart(id=_TCID, output="the tool ran", error=False)


@pytest.mark.asyncio
async def test_an_approved_gate_that_completes_is_unchanged():
    part = await _resume_with({"decision": "approved"}, resumed=_tool_runs)

    assert part.id == _AGENT_TC
    assert part.error is False
    assert json.loads(part.output) == {"output": "the tool ran"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"decision": "rejected", "reason": "no thanks"}, "no thanks"),
        ({}, "malformed approval payload (missing decision)"),
        ({"__yield_timeout__": True, "elapsed_seconds": 3.0}, "timed-out"),
        ({"__yield_cancelled__": True, "reason": "superseded"}, "superseded"),
    ],
    ids=["rejected", "malformed", "timeout", "cancelled"],
)
async def test_a_refused_gate_in_a_child_graph_is_the_flat_paths_error_result(payload, reason):
    part = await _resume_with(payload, resumed=_never_re_dispatched)

    assert part.id == _AGENT_TC, "the result must pair with the agent's invoke_graph call"
    assert part.error is True, f"a refusal reached the agent as a success: {part.output!r}"
    assert json.loads(part.output) == {"rejected": True, "reason": reason, "tool_name": _GATED_TOOL}


@pytest.mark.asyncio
async def test_a_tool_that_crashes_after_approval_is_an_error_result():
    async def crashes(node, arguments, bypass_approval=False):
        raise RuntimeError("disk on fire")

    part = await _resume_with({"decision": "approved"}, resumed=crashes)

    assert part.id == _AGENT_TC
    assert part.error is True, f"a tool crash reached the agent as a success: {part.output!r}"
    body = json.loads(part.output)
    assert body == {"error": "tool_execution_failed", "message": "disk on fire", "node_id": "ask"}


@pytest.mark.asyncio
async def test_a_routing_failure_after_approval_is_an_error_result():
    async def answers_without_a_match(node, arguments, bypass_approval=False):
        return ToolResultPart(id=_TCID, output=json.dumps({"go": "miss"}), error=False)

    part = await _resume_with({"decision": "approved"}, resumed=answers_without_a_match, graph=_routing_graph())

    assert part.error is True, f"a routing failure reached the agent as a success: {part.output!r}"
    body = json.loads(part.output)
    assert body["error"] == "routing_failed" and body["node_id"] == "ask", body
    assert body["message"], "the failure carries no message"


@pytest.mark.asyncio
async def test_a_child_graph_that_fails_on_its_first_run_is_an_error_result_from_the_tool():
    async def crashes(node, arguments):
        raise RuntimeError("disk on fire")

    graph = build_ask_user_graph()
    (executor,) = await _executors(graph, crashes)
    ctx = SimpleNamespace(
        session_id=_SESSION_ID, workspace_id="ws-1", tool_call_id=_AGENT_TC, graph_services=_services(graph, [executor]),
    )

    result = await _invoke_graph_handler({"graph_id": "child", "input": "go"}, ctx=ctx)

    assert isinstance(result, ToolCallResult)
    assert result.is_error is True, f"a failed child graph reached the agent as a success: {result.output!r}"
    body = json.loads(result.output)
    assert body["error"] == "tool_execution_failed" and body["node_id"] == "ask", body
    assert "disk on fire" in body["message"]
