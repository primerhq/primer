"""An edge that cannot be evaluated after a RESUME ends the graph ``failed``, as it does in the main loop (ticket 01a12625-436c).

The main loop catches the ``ConfigError`` of ``_compute_next_ready`` (a router that is not registered, one that names a node that does not exist), logs it and ends the run ``failed``. The same call in
``resume_from_checkpoint`` caught only ``_RoutingFailed``, so the ``ConfigError`` left the method as a raw exception, the thread was never saved as ended and ``last_done_reason`` stayed ``None``. It now
ends the run ``failed`` with no detail (like the main loop) and remembers the text of the failure for ``primer.graph.invoke_graph``, which tells the invoking agent what failed.

With real executors: a real ``GraphExecutor`` parks a ToolCall node at an approval gate and a fresh one resumes it, over a graph whose conditional edge names a router that is not registered.
"""

from __future__ import annotations

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _BeginNode, _CallableRouter, _ConditionalEdge, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.workspace_session import SessionStatus
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.graph_end import graph_end_for
from tests.graph.test_toolcall_dispatch import _InMemoryStorage


def _graph_with_an_unregistered_router() -> Graph:
    return Graph(
        id="g-router-unregistered", description="begin -> t -> a conditional edge whose router is not registered -> exit",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="t", tool_id="dangerous__tool", arguments={}), _EndNode(id="exit", output_template="done")],
        edges=[_StaticEdge(from_node="begin", to_node="t"), _ConditionalEdge(from_node="t", router=_CallableRouter(callable_id="not-registered"))],
    )


async def _park_then_resume():
    gate = Yielded(tool_name="_approval", event_key="tool_approval:sid:tc-1")

    async def first(node, arguments):
        raise YieldToWorker(gate, tool_call_id="tc-1")

    async def approved(node, arguments, bypass_approval=False):
        from primer.model.chat import ToolResultPart

        return ToolResultPart(id="tc-1", output="the tool ran", error=False)

    async def agent_resolver(agent_id):
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    graph = _graph_with_an_unregistered_router()
    ts, ms = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts)
    ex1 = GraphExecutor(graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms, graph_thread_id=thread.id, tool_dispatcher=first)
    with pytest.raises(YieldToWorker) as parked:
        async for _ in ex1.invoke([]):
            pass
    ex2 = GraphExecutor(graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms, graph_thread_id=thread.id, tool_dispatcher=approved)
    events = [ev async for ev in ex2.resume_from_checkpoint(parked.value.graph_checkpoint)]
    return ex2, events, await ts.get(thread.id)


@pytest.mark.asyncio
async def test_an_edge_that_cannot_be_evaluated_after_a_resume_ends_the_graph_failed() -> None:
    ex, _, saved = await _park_then_resume()

    assert (saved.status, saved.ended_reason, saved.ended_detail) == (SessionStatus.ENDED, "failed", None), "the thread was not saved as ended (the resume raised)"
    assert ex.last_done_reason == "graph_failed"
    record = graph_end_for(ex.last_done_reason, ex)
    assert record is not None and record.payload["raw_reason"] == "graph_failed" and record.payload["ended_reason"] == "failed"


@pytest.mark.asyncio
async def test_the_executor_remembers_which_edge_failed() -> None:
    ex, _, _ = await _park_then_resume()

    assert "not-registered" in (ex._last_failure_text or ""), ex._last_failure_text
