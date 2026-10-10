"""A graph edited while it is parked, whose pending ToolCall node is now another kind of node, ends FAILED when it is resumed (ticket 01a125a1-3591, T1 of the #701 round 4 review).

The resume marks the pending entry's node ``FAILED`` ("topology drifted between checkpoint + resume") but then carried on: the branch is not one of the early exits that save ``ENDED/failed`` and return, so
the main loop ran to its tail with nothing failed in the superstep and ended the run ``completed``. The thread said ``ENDED/completed``, ``last_done_reason`` was ``graph_ended`` and ``end_graph`` wrote
``done(stop, graph_ended, completed)`` for a graph whose node had failed (the same truthfulness class as the eight exits of #701 round 4). It must leave like they do: a ``topology_drift`` error event
(``resume_invoke_graph`` decides a resumed child by the error event it yields, so a drifted child graph must yield one), the node's balanced ``failed`` exit, and ``ENDED/failed`` with the detail.

With real executors: a real ``GraphExecutor`` parks a ToolCall node at an approval gate, a fresh one built on the EDITED graph (the same ids, ``t`` now an End node) resumes it.
"""

from __future__ import annotations

import pytest

from primer.graph.base import _GraphErrorEvent, _GraphTransitionEvent
from primer.graph.executor import GraphExecutor
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.workspace_session import SessionStatus
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.graph_end import graph_end_for
from tests.graph.test_toolcall_dispatch import _InMemoryStorage


def _graph_with_a_tool_node() -> Graph:
    return Graph(
        id="g", description="begin -> t -> exit",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="t", tool_id="dangerous__tool", arguments={}), _EndNode(id="exit", output_template="done")],
        edges=[_StaticEdge(from_node="begin", to_node="t"), _StaticEdge(from_node="t", to_node="exit")],
    )


def _graph_where_t_is_an_end_node() -> Graph:
    """The same ids; ``t`` was edited into an End node while the run was parked."""
    return Graph(
        id="g", description="begin -> t(end)",
        nodes=[_BeginNode(id="begin"), _EndNode(id="t", output_template="drifted")],
        edges=[_StaticEdge(from_node="begin", to_node="t")],
    )


async def _park_then_resume_on(edited: Graph):
    gate = Yielded(tool_name="_approval", event_key="tool_approval:sid:tc-1")

    async def first(node, arguments):
        raise YieldToWorker(gate, tool_call_id="tc-1")

    async def never(node, arguments, bypass_approval=False):
        raise AssertionError("a drifted node is not dispatched")

    async def agent_resolver(agent_id):
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    original = _graph_with_a_tool_node()
    ts, ms = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=original, thread_storage=ts)
    ex1 = GraphExecutor(graph=original, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms, graph_thread_id=thread.id, tool_dispatcher=first)
    with pytest.raises(YieldToWorker) as parked:
        async for _ in ex1.invoke([]):
            pass
    ex2 = GraphExecutor(graph=edited, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms, graph_thread_id=thread.id, tool_dispatcher=never)
    events = [ev async for ev in ex2.resume_from_checkpoint(parked.value.graph_checkpoint)]
    return ex2, events, await ts.get(thread.id)


@pytest.mark.asyncio
async def test_a_pending_tool_node_that_is_another_kind_of_node_ends_the_resumed_graph_failed() -> None:
    ex, events, saved = await _park_then_resume_on(_graph_where_t_is_an_end_node())

    assert (saved.status, saved.ended_reason, saved.ended_detail) == (SessionStatus.ENDED, "failed", "topology_drift"), "the saved state says the graph completed although a node failed"
    assert ex.last_done_reason == "graph_failed", "the executor does not know how the resumed run ended"
    record = graph_end_for(ex.last_done_reason, ex)
    assert record is not None
    assert record.payload["stop_reason"] == "error" and record.payload["raw_reason"] == "graph_failed"
    assert record.payload["ended_reason"] == "failed" and record.payload["graph_end"] is True
    assert ex._node_states["t"].status.value == "failed"


@pytest.mark.asyncio
async def test_the_drift_is_an_error_event_naming_the_node_and_the_exit_of_the_node_is_balanced() -> None:
    _, events, _ = await _park_then_resume_on(_graph_where_t_is_an_end_node())

    errors = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert [(e.code, e.node_id) for e in errors] == [("topology_drift", "t")], "a resumed child graph is judged failed by its error event: the drift must yield one"
    assert "t" in errors[0].message and "_ToolCallNode" in errors[0].message, errors[0].message
    exits = [(ev.node_id, ev.phase, ev.status) for ev in events if isinstance(ev, _GraphTransitionEvent) and ev.node_id == "t"]
    assert exits == [("t", "exit", "failed")], f"the parked node's exit is announced once, failed: {exits}"
