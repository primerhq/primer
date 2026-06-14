"""Approval gate on a *yielding* tool_call node - two-phase park.

When a ToolCall node's underlying tool is BOTH approval-gated AND a
yielding tool, the park is two-phase:

* Phase 1 - the call parks for the operator's approval decision.
* Phase 2 - once APPROVED, the bypassed re-dispatch runs the real tool,
  which itself yields for its own event (timer/file/graph/human). The
  resume drain must NOT swallow this second :class:`YieldToWorker` as a
  node failure - it must RE-PARK on the new event key.

A REJECT still short-circuits to ``tool_execution_failed`` (the tool
never runs), which :mod:`tests.graph.test_toolcall_approval_reject`
already covers; we re-assert it here for the yielding-tool shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.graph.base import _GraphErrorEvent, _ToolApprovalRejected
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent
from primer.model.chat import StreamEvent
from primer.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    NodeOutput,
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


def _build_graph() -> Graph:
    return Graph(
        id="g-yield-approval",
        description="begin -> tool(approval+yield) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="t",
                tool_id="workspace_ext__sleep",
                arguments={"seconds": 30},
            ),
            _EndNode(id="exit", output_template="done"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )


async def _agent_resolver(agent_id: str) -> Agent:
    raise KeyError(agent_id)


async def _llm_resolver(agent):  # pragma: no cover - never reached
    raise NotImplementedError


@pytest.mark.asyncio
async def test_approved_yielding_toolcall_reparks_on_real_event() -> None:
    """Approve a yielding tool_call node -> the resume RE-PARKS on the
    tool's own event key (phase 2), it does NOT mark the node failed."""
    graph = _build_graph()

    approval_yield = Yielded(
        tool_name="_approval",
        event_key="tool_approval:sid:tc-sleep",
    )
    # The real tool's own yield (phase 2): a timer event, NOT an approval.
    real_event_yield = Yielded(
        tool_name="workspace_ext__sleep",
        event_key="timer:tc-sleep",
        resume_metadata={"seconds": 30},
    )

    async def first_dispatcher(node, arguments):
        # Phase 1: the approval gate fires.
        raise YieldToWorker(approval_yield, tool_call_id="tc-sleep")

    async def approved_yielding_dispatch(node, arguments, bypass_approval=False):
        # Phase 2: approved -> the real tool runs and yields for its event.
        raise YieldToWorker(real_event_yield, tool_call_id="tc-sleep")

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(
        GraphNodeMessage
    )
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=first_dispatcher,
    )

    _events, raised = await _drain_until_yield(executor.invoke([]))
    assert raised is not None
    # Phase 1 parked on the approval key.
    assert raised.yielded.event_key == "tool_approval:sid:tc-sleep"
    payload = executor.snapshot_state()

    executor2 = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=approved_yielding_dispatch,
    )
    resume_events, repark = await _drain_until_yield(
        executor2.resume_from_checkpoint(payload)
    )

    # Phase 2: the resume RE-PARKED on the tool's real event key - no
    # failure event, no tool_execution_failed node output.
    assert repark is not None, "approved yielding tool should re-park, not error"
    assert repark.yielded.event_key == "timer:tc-sleep"
    assert not [e for e in resume_events if isinstance(e, _GraphErrorEvent)]

    node_out = executor2._context.nodes.get("t") if executor2._context else None
    assert node_out is None or getattr(node_out, "ended_detail", None) != (
        "tool_execution_failed"
    )
    # The node is still pending (re-parked), not drained.
    assert any(
        p.tool_call_id == "tc-sleep" for p in executor2._pending_toolcalls
    )


@pytest.mark.asyncio
async def test_rejected_yielding_toolcall_short_circuits_to_failure() -> None:
    """Reject the approval on a yielding tool -> tool_execution_failed; the
    real tool never runs."""
    graph = _build_graph()

    approval_yield = Yielded(
        tool_name="_approval", event_key="tool_approval:sid:tc-sleep"
    )

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(approval_yield, tool_call_id="tc-sleep")

    ran = {"dispatched": False}

    async def reject_dispatcher(node, arguments, bypass_approval=False):
        ran["dispatched"] = True  # must NOT happen
        raise _ToolApprovalRejected("operator rejected", tool_call_id="tc-sleep")

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(
        GraphNodeMessage
    )
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=first_dispatcher,
    )
    _e, raised = await _drain_until_yield(executor.invoke([]))
    assert raised is not None
    payload = executor.snapshot_state()

    executor2 = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=reject_dispatcher,
    )
    resume_events = await _drain(executor2.resume_from_checkpoint(payload))

    errs = [e for e in resume_events if isinstance(e, _GraphErrorEvent)]
    assert len(errs) == 1
    assert errs[0].code == "tool_execution_failed"
    node_out = executor2._context.nodes.get("t") if executor2._context else None
    assert isinstance(node_out, NodeOutput)
    assert node_out.ended_detail == "tool_execution_failed"
