"""Phase 11 Task 11.1 — graph-parked session resume.

When a graph-bound session parks at a ToolCall approval gate (Phase 6
stamped ``YieldToWorker.graph_checkpoint`` on the way out), the
worker's resume path must dispatch to
:func:`primer.worker.graph_resume.resume_graph_from_checkpoint` rather
than the agent ``inject_resume_messages`` path. This test exercises
the adapter directly so the wiring stays decoupled from the full pool
turn loop while still pinning the load-bearing contract:

* :attr:`ParkedState.graph_checkpoint` round-trips through
  :meth:`to_jsonable` / :meth:`from_jsonable`.
* On the approved path, the adapter drains the executor's
  ``resume_from_checkpoint`` stream and runs the pending ToolCall(s)
  with ``bypass_approval=True``.
* On the rejected path, the adapter monkeypatches the executor's
  ``_dispatch_toolcall_with_bypass`` to raise ``_ToolApprovalRejected``
  so the graph terminates ``failed`` per spec §4.8.
* The agent-park codepath is unaffected (regression guard).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent
from primer.model.chat import StreamEvent, ToolResultPart
from primer.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    _BeginNode,
    _EndNode,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.yield_ import (
    YieldCancelled,
    YieldTimeout,
    YieldToWorker,
    Yielded,
)
from primer.worker.graph_resume import (
    _decision_from_payload,
    resume_graph_from_checkpoint,
)
from primer.worker.yield_runtime import ParkedState

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


def _make_simple_graph(graph_id: str) -> Graph:
    return Graph(
        id=graph_id,
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


async def _agent_resolver(agent_id: str) -> Agent:
    raise KeyError(agent_id)


async def _llm_resolver(agent):
    raise NotImplementedError


# ===========================================================================
# Decision classification
# ===========================================================================


def test_decision_from_payload_approved_dict() -> None:
    decision, reason = _decision_from_payload({"decision": "approved"})
    assert decision == "approved"
    assert reason is None


def test_decision_from_payload_approved_with_reason() -> None:
    decision, reason = _decision_from_payload(
        {"decision": "approved", "reason": "ok"}
    )
    assert decision == "approved"
    assert reason == "ok"


def test_decision_from_payload_rejected_dict() -> None:
    decision, reason = _decision_from_payload(
        {"decision": "rejected", "reason": "no thanks"}
    )
    assert decision == "rejected"
    assert reason == "no thanks"


def test_decision_from_payload_yield_timeout() -> None:
    decision, reason = _decision_from_payload(YieldTimeout(elapsed_seconds=3600))
    assert decision == "rejected"
    assert reason == "timed-out"


def test_decision_from_payload_yield_cancelled() -> None:
    payload = YieldCancelled(
        reason="changed-my-mind",
        cancelled_at=datetime.now(timezone.utc),
        elapsed_seconds=12.0,
    )
    decision, reason = _decision_from_payload(payload)
    assert decision == "rejected"
    assert reason == "changed-my-mind"


def test_decision_from_payload_malformed() -> None:
    decision, reason = _decision_from_payload({"foo": "bar"})
    assert decision == "rejected"
    assert reason and "missing decision" in reason


def test_decision_from_payload_non_dict() -> None:
    decision, reason = _decision_from_payload("not-a-dict")
    assert decision == "rejected"
    assert reason and "non-dict" in reason


# ===========================================================================
# ParkedState.graph_checkpoint round-trip
# ===========================================================================


def test_parked_state_graph_checkpoint_roundtrip() -> None:
    """The new field round-trips through JSON without loss."""
    yielded = Yielded(
        tool_name="_approval",
        event_key="tool_approval:s:tc-1",
        resume_metadata={"original_call": {"id": "tc-1", "name": "x", "arguments": {}}},
    )
    checkpoint = {
        "context": {"iteration": 1, "nodes": {}},
        "ready_set": ["t"],
        "pending_toolcalls": [
            {
                "node_id": "t",
                "tool_call_id": "tc-1",
                "parked_event_key": "tool_approval:s:tc-1",
                "arguments": {"q": "x"},
            }
        ],
    }
    state = ParkedState(
        yielded=yielded,
        llm_messages=[],
        turn_no=1,
        started_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
        tool_call_id="tc-1",
        graph_checkpoint=checkpoint,
    )

    raw = state.to_jsonable()
    assert raw["graph_checkpoint"] == checkpoint

    restored = ParkedState.from_jsonable(raw)
    assert restored.graph_checkpoint == checkpoint


def test_parked_state_graph_checkpoint_optional() -> None:
    """Agent parks (no graph_checkpoint) round-trip with None."""
    yielded = Yielded(tool_name="sleep", event_key="timer:tc-1")
    state = ParkedState(
        yielded=yielded,
        llm_messages=[],
        turn_no=1,
        started_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    raw = state.to_jsonable()
    assert raw["graph_checkpoint"] is None
    restored = ParkedState.from_jsonable(raw)
    assert restored.graph_checkpoint is None


# ===========================================================================
# Adapter: approved path drains the executor
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_graph_from_checkpoint_approved_drains() -> None:
    """Approved decision → executor's bypassed dispatch runs to completion."""
    graph = _make_simple_graph("g-resume-approved")
    yielded_obj = Yielded(
        tool_name="_approval", event_key="tool_approval:sid:tc-1"
    )

    bypass_log: list[bool] = []

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(yielded_obj, tool_call_id="tc-1")

    async def resume_dispatcher(node, arguments, bypass_approval=False):
        bypass_log.append(bypass_approval)
        return ToolResultPart(id="tc-1", output="ok")

    thread_storage = _InMemoryStorage(GraphThread)
    message_storage = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    park_executor = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=first_dispatcher,
    )
    _events, raised = await _drain_until_yield(park_executor.invoke([]))
    assert raised is not None
    checkpoint = park_executor.snapshot_state()

    # Round-trip through ParkedState the way the real park-write does.
    parked = ParkedState(
        yielded=raised.yielded,
        llm_messages=[],
        turn_no=1,
        started_at=datetime.now(timezone.utc),
        tool_call_id=raised.tool_call_id,
        graph_checkpoint=checkpoint,
    )
    restored = ParkedState.from_jsonable(parked.to_jsonable())
    assert restored.graph_checkpoint is not None

    # Stream taps: capture events on the resume executor.
    resume_executor = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=resume_dispatcher,
    )

    # Capture events from the resume drain so we can assert on End.
    captured: list[StreamEvent] = []

    real_resume = resume_executor.resume_from_checkpoint

    async def _tap(cp, **kw):
        async for ev in real_resume(cp, **kw):
            captured.append(ev)
            yield ev

    resume_executor.resume_from_checkpoint = _tap  # type: ignore[assignment]

    decision, _repark = await resume_graph_from_checkpoint(
        executor=resume_executor,
        checkpoint=restored.graph_checkpoint,  # type: ignore[arg-type]
        payload={"decision": "approved"},
    )
    assert decision == "approved"
    # The bypass-approval dispatcher fired exactly once with bypass=True.
    assert bypass_log == [True]

    # End event surfaces the tool result through output_template.
    end_outputs = [e for e in captured if isinstance(e, _GraphEndOutputEvent)]
    assert len(end_outputs) == 1
    assert end_outputs[0].text == "ok"

    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "completed"


# ===========================================================================
# Adapter: rejected path stamps tool_execution_failed
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_graph_from_checkpoint_rejected_terminates_failed() -> None:
    """Rejected decision → adapter raises ``_ToolApprovalRejected`` inside
    the executor's bypass dispatch; resume drain emits the terminal error
    event and the thread ends ``failed``.
    """
    graph = _make_simple_graph("g-resume-rejected")
    yielded_obj = Yielded(
        tool_name="_approval", event_key="tool_approval:sid:tc-1"
    )

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(yielded_obj, tool_call_id="tc-1")

    async def never_called(node, arguments, bypass_approval=False):
        raise AssertionError("rejection path should not call the dispatcher")

    thread_storage = _InMemoryStorage(GraphThread)
    message_storage = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    park_executor = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=first_dispatcher,
    )
    _events, raised = await _drain_until_yield(park_executor.invoke([]))
    assert raised is not None
    checkpoint = park_executor.snapshot_state()

    resume_executor = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=never_called,
    )

    captured: list[StreamEvent] = []
    real_resume = resume_executor.resume_from_checkpoint

    async def _tap(cp, **kw):
        async for ev in real_resume(cp, **kw):
            captured.append(ev)
            yield ev

    resume_executor.resume_from_checkpoint = _tap  # type: ignore[assignment]

    decision, _repark = await resume_graph_from_checkpoint(
        executor=resume_executor,
        checkpoint=checkpoint,
        payload={"decision": "rejected", "reason": "no thanks"},
    )
    assert decision == "rejected"

    errs = [e for e in captured if isinstance(e, _GraphErrorEvent)]
    assert len(errs) == 1
    assert errs[0].code == "tool_execution_failed"
    assert errs[0].node_id == "t"

    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "tool_execution_failed"


@pytest.mark.asyncio
async def test_resume_graph_from_checkpoint_timeout_terminates_failed() -> None:
    """YieldTimeout payload is treated as a rejection."""
    graph = _make_simple_graph("g-resume-timeout")
    yielded_obj = Yielded(
        tool_name="_approval", event_key="tool_approval:sid:tc-1"
    )

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(yielded_obj, tool_call_id="tc-1")

    async def never_called(node, arguments, bypass_approval=False):
        raise AssertionError("timeout path should not call the dispatcher")

    thread_storage = _InMemoryStorage(GraphThread)
    message_storage = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    park_executor = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=first_dispatcher,
    )
    _events, raised = await _drain_until_yield(park_executor.invoke([]))
    assert raised is not None
    checkpoint = park_executor.snapshot_state()

    resume_executor = GraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=never_called,
    )

    decision, _repark = await resume_graph_from_checkpoint(
        executor=resume_executor,
        checkpoint=checkpoint,
        payload=YieldTimeout(elapsed_seconds=3600.0),
    )
    assert decision == "rejected"

    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "tool_execution_failed"
