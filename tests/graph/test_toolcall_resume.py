"""Phase 6 Task 6.3 — resume from a graph checkpoint.

After an operator approves a yielded ToolCall, the worker spins up a
fresh executor and calls :meth:`resume_from_checkpoint(payload)`. The
executor:

* restores its mid-flight state from the payload
* re-dispatches every pending ToolCall with ``bypass_approval=True``
  (so the approval gate doesn't fire again)
* records the resulting NodeOutputs in GraphContext
* resumes the regular superstep loop and runs to completion
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.graph.base import _GraphEndOutputEvent
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


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


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
async def test_resume_from_checkpoint_drains_pending_toolcalls() -> None:
    """First invoke yields; resume from checkpoint with bypass_approval=True."""
    graph = Graph(
        id="g-resume",
        description="begin -> tool -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="t",
                tool_id="dangerous__tool",
                arguments={"q": "x"},
            ),
            _EndNode(id="exit", output_template="{{ nodes.t.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    yielded_obj = Yielded(
        tool_name="_approval",
        event_key="tool_approval:sid:tc-1",
    )

    # The first dispatcher raises (yields). The second (after approval)
    # records bypass_approval and returns the real result.
    call_log: list[tuple[str, dict, bool]] = []

    async def first_dispatcher(node, arguments):
        call_log.append((node.tool_id, dict(arguments), False))
        raise YieldToWorker(yielded_obj, tool_call_id="tc-1")

    async def resume_dispatcher(node, arguments, bypass_approval=False):
        call_log.append((node.tool_id, dict(arguments), bypass_approval))
        return ToolResultPart(id="tc-1", output="approved-output")

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
        tool_dispatcher=first_dispatcher,
    )

    _events, raised = await _drain_until_yield(executor.invoke([]))
    assert raised is not None
    payload = executor.snapshot_state()

    # Build a fresh executor for the resume path.
    executor2 = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=resume_dispatcher,
    )

    resume_events = await _drain(executor2.resume_from_checkpoint(payload))

    # The dispatcher was called once with bypass_approval=True during the
    # resume drain.
    assert any(c[2] is True for c in call_log if c[0] == "dangerous__tool"), (
        f"resume dispatcher should have been called with bypass_approval=True: {call_log}"
    )

    # Graph completed; End output is the ToolResult.output.
    end_outputs = [e for e in resume_events if isinstance(e, _GraphEndOutputEvent)]
    assert len(end_outputs) == 1
    assert end_outputs[0].text == "approved-output"

    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "completed"
    # Pending list cleared.
    assert executor2._pending_toolcalls == []
