"""Phase 6 Task 6.2 — ToolCall yields → checkpoint + propagate YieldToWorker.

When a ToolCall node's dispatcher raises :class:`YieldToWorker` mid-graph,
the executor must:

1. Defer the ToolCall (don't fail it) so the rest of the superstep can
   continue computing.
2. After the superstep settles, save a checkpoint and re-raise
   ``YieldToWorker`` upward so the worker parks the session.
3. The checkpoint payload from ``snapshot_state`` round-trips into a
   fresh executor via ``restore_state``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent
from primer.model.chat import Message, StreamEvent, ToolResultPart
from primer.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    _BeginNode,
    _EndNode,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.yield_ import Yielded, YieldToWorker

from tests.graph.test_toolcall_dispatch import _InMemoryStorage


async def _drain_until_yield(
    it: AsyncIterator[StreamEvent],
) -> tuple[list[StreamEvent], YieldToWorker | None]:
    events: list[StreamEvent] = []
    try:
        async for ev in it:
            events.append(ev)
    except YieldToWorker as exc:
        return events, exc
    return events, None


@pytest.mark.asyncio
async def test_toolcall_yield_writes_checkpoint_and_propagates() -> None:
    """Single ToolCall node yielding for approval → checkpoint + YieldToWorker."""
    graph = Graph(
        id="g-yield",
        description="begin -> tool(yields) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="t",
                tool_id="dangerous__tool",
                arguments={"q": "x"},
            ),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    yielded_obj = Yielded(
        tool_name="_approval",
        event_key="tool_approval:sid-1:tc-1",
        resume_metadata={"policy_id": "p-1"},
    )

    async def stub_dispatcher(node, arguments):
        raise YieldToWorker(yielded_obj, tool_call_id="tc-1")

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=stub_dispatcher,
    )

    events, raised = await _drain_until_yield(executor.invoke([]))

    # Executor surfaced the YieldToWorker upward so the worker can park.
    assert raised is not None
    assert raised.tool_call_id == "tc-1"
    assert raised.yielded.event_key == "tool_approval:sid-1:tc-1"

    # Pending ToolCall captured.
    assert len(executor._pending_toolcalls) == 1
    p = executor._pending_toolcalls[0]
    assert p.node_id == "t"
    assert p.tool_call_id == "tc-1"
    assert p.parked_event_key == "tool_approval:sid-1:tc-1"
    assert p.arguments == {"q": "x"}

    # Checkpoint round-trip works.
    payload = executor.snapshot_state()
    import json
    json.dumps(payload)

    # Spawn a second executor and restore.
    thread2 = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor2 = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread2.id,
        tool_dispatcher=stub_dispatcher,
    )
    executor2.restore_state(payload)
    assert len(executor2._pending_toolcalls) == 1
    assert executor2._pending_toolcalls[0].tool_call_id == "tc-1"

    # The yield should fire before End ran — context shouldn't have "exit".
    assert executor._context is not None
    assert "exit" not in executor._context.nodes
    # Begin output should be there (it ran in iter 0 before the toolcall).
    assert "begin" in executor._context.nodes


@pytest.mark.asyncio
async def test_toolcall_yield_does_not_mark_node_failed() -> None:
    """The yielded ToolCall node's runtime status stays RUNNING, not FAILED."""
    from primer.model.graph import NodeRuntimeStatus

    graph = Graph(
        id="g-yield2",
        description="begin -> tool(yields) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="t",
                tool_id="dangerous__tool",
                arguments={"q": "x"},
            ),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    yielded_obj = Yielded(
        tool_name="_approval",
        event_key="tool_approval:sid-2:tc-2",
    )

    async def stub_dispatcher(node, arguments):
        raise YieldToWorker(yielded_obj, tool_call_id="tc-2")

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=stub_dispatcher,
    )

    _events, raised = await _drain_until_yield(executor.invoke([]))
    assert raised is not None

    # The thread's persisted state should NOT mark the toolcall as FAILED;
    # status is RUNNING (suspended pending approval).
    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    t_state = loaded.node_states.get("t")
    assert t_state is not None
    assert t_state.status == NodeRuntimeStatus.RUNNING


@pytest.mark.asyncio
async def test_toolcall_yield_carries_original_call_metadata() -> None:
    """The re-raised approval yield must carry original_call (tool name +
    arguments) so the channel / approval UI shows what is being approved,
    not 'Approve <unknown>({})?'. The graph rebuilds it from the node's
    tool_id and the pending entry's rendered arguments."""
    graph = Graph(
        id="g-meta",
        description="begin -> tool(yields) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="t", tool_id="workspace__write",
                          arguments={"path": "release.txt", "content": "hi"}),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    # The gate raises with original_call (as ToolExecutionManager does); the
    # graph must NOT drop it on the outer re-raise.
    async def stub_dispatcher(node, arguments):
        raise YieldToWorker(
            Yielded(tool_name="_approval",
                    event_key="tool_approval:sid-1:tc-9",
                    resume_metadata={"original_call": {
                        "id": "tc-9", "name": "workspace__write",
                        "arguments": arguments}}),
            tool_call_id="tc-9")

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor = GraphExecutor(
        graph=graph, agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id, tool_dispatcher=stub_dispatcher,
    )

    _events, raised = await _drain_until_yield(executor.invoke([]))
    assert raised is not None
    oc = (raised.yielded.resume_metadata or {}).get("original_call")
    assert oc is not None, "outer approval yield dropped original_call"
    assert oc["name"] == "workspace__write"
    assert oc["arguments"] == {"path": "release.txt", "content": "hi"}
