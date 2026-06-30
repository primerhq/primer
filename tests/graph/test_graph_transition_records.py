"""Graph-runtime ``graph_transition`` records flow into the session log.

Spec §2.6 / plan Task 3.1. The superstep loop emits a
``_GraphTransitionEvent`` at every node ENTER (``phase='enter'``,
``status=None``) and EXIT (``phase='exit'``, ``status='completed'`` /
``'failed'``). The session-layer translator
(:func:`primer.session.persistence.translate_stream_event`) turns each
into a ``SessionMessageRecord(kind=graph_transition, payload={node_id,
node_kind, phase, status})`` which lands in ``messages.jsonl`` via the
same :class:`WorkspaceMessageWriter.append` path every other record uses,
and which :func:`primer.tap.event.record_to_tap_event` maps 1:1 onto
:attr:`primer.tap.event.TapEventClass.GRAPH_TRANSITION`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphTransitionEvent, _ToolApprovalRejected
from primer.graph.executor import GraphExecutor
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, StreamEvent, TextDelta, ToolResultPart
from primer.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.provider import LLMModel
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.persistence import (
    WorkspaceMessageWriter,
    _CoalesceState,
    translate_stream_event,
)
from primer.tap.event import TapEventClass, record_to_tap_event
from primer.workspace.local.state import LocalStateRepo as StateRepo

from tests.graph.test_toolcall_dispatch import _InMemoryStorage


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Streams a fixed script per call (one assistant turn => stop)."""

    def __init__(self, *, script: list[StreamEvent]) -> None:
        self._script = script

    async def list_models(self) -> list[str]:
        return ["m"]

    def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        async def _impl() -> AsyncIterator[StreamEvent]:
            for ev in self._script:
                yield ev

        return _impl()


class _FakeWorkspaceIO:
    """In-memory ``WorkspaceIO`` capturing every appended jsonl line."""

    def __init__(self) -> None:
        self.buffers: dict[str, bytearray] = {}

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.buffers.setdefault(session_id, bytearray()).extend(line)

    async def append_state_line(
        self, workspace_id: str, relative_path: str, line: bytes,
    ) -> None:  # pragma: no cover - unused by these tests
        return None

    def lines(self, session_id: str) -> list[str]:
        raw = bytes(self.buffers.get(session_id, b""))
        return [s for s in raw.decode("utf-8").split("\n") if s]


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _make_state_repo(tmp_path: Path) -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-test")
    await repo.initialize()
    return repo


async def _build_executor(
    *, graph: Graph, tmp_path: Path, graph_session_id: str,
) -> WorkspaceGraphExecutor:
    repo = await _make_state_repo(tmp_path)
    agents = {"n1": _agent("n1")}
    llm = _FakeLLM(
        script=[
            TextDelta(text="hi", index=0),
            Done(stop_reason="stop", raw_reason="stop"),
        ]
    )

    async def agent_resolver(agent_id: str) -> Agent:
        return agents[agent_id]

    async def llm_resolver(_a: Agent):
        return (llm, _model())

    return WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id=graph_session_id,
    )


def _begin_agent_end_graph() -> Graph:
    """begin -> agent(n1) -> end."""
    return Graph(
        id="g-transition",
        description="begin -> agent(n1) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="n1", agent_id="n1"),
            _EndNode(id="end", output_template="done"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="n1"),
            _StaticEdge(from_node="n1", to_node="end"),
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_emits_transition_events_per_node(tmp_path: Path) -> None:
    """A begin -> agent(n1) -> end run yields one enter + one exit
    ``_GraphTransitionEvent`` per node, in node order, with the node's
    ``kind`` and a populated exit status."""
    executor = await _build_executor(
        graph=_begin_agent_end_graph(),
        tmp_path=tmp_path,
        graph_session_id="gs-events",
    )

    transitions: list[_GraphTransitionEvent] = []
    async for ev in executor.invoke([]):
        if isinstance(ev, _GraphTransitionEvent):
            transitions.append(ev)

    seen = [(t.node_id, t.node_kind, t.phase, t.status) for t in transitions]
    # Each node fires exactly one enter (status None) and one exit
    # (status 'completed'). Order follows the begin -> n1 -> end topology.
    assert seen == [
        ("begin", "begin", "enter", None),
        ("begin", "begin", "exit", "completed"),
        ("n1", "agent", "enter", None),
        ("n1", "agent", "exit", "completed"),
        ("end", "end", "enter", None),
        ("end", "end", "exit", "completed"),
    ], seen


@pytest.mark.asyncio
async def test_transition_records_land_in_messages_jsonl(tmp_path: Path) -> None:
    """Translated through the session writer, each transition lands as a
    ``graph_transition`` record carrying ``node_id`` / ``node_kind`` /
    ``phase`` / ``status`` in the per-session message log."""
    executor = await _build_executor(
        graph=_begin_agent_end_graph(),
        tmp_path=tmp_path,
        graph_session_id="gs-jsonl",
    )

    fake_io = _FakeWorkspaceIO()
    writer = WorkspaceMessageWriter(workspace_io=fake_io, session_id="gs-jsonl")
    coalesce = _CoalesceState()
    async for ev in executor.invoke([]):
        rec = translate_stream_event(ev, coalesce)
        if rec is None:
            continue
        recs = rec if isinstance(rec, list) else [rec]
        for r in recs:
            await writer.append(r)
    await writer.flush()

    records = [json.loads(line) for line in fake_io.lines("gs-jsonl")]
    transitions = [
        r for r in records
        if r["kind"] == SessionMessageKind.GRAPH_TRANSITION.value
    ]

    # The agent node n1 has a clear enter + exit; assert both are present
    # with the expected payload shape.
    n1_enter = [
        r for r in transitions
        if r["payload"]["node_id"] == "n1" and r["payload"]["phase"] == "enter"
    ]
    n1_exit = [
        r for r in transitions
        if r["payload"]["node_id"] == "n1" and r["payload"]["phase"] == "exit"
    ]
    assert len(n1_enter) == 1, transitions
    assert len(n1_exit) == 1, transitions
    assert n1_enter[0]["payload"] == {
        "node_id": "n1",
        "node_kind": "agent",
        "phase": "enter",
        "status": None,
    }
    assert n1_exit[0]["payload"] == {
        "node_id": "n1",
        "node_kind": "agent",
        "phase": "exit",
        "status": "completed",
    }
    # seq is writer-assigned and monotonic; enter precedes exit.
    assert n1_enter[0]["seq"] < n1_exit[0]["seq"]


# ---------------------------------------------------------------------------
# Resume-path EXIT balance (Spec B §2.3 / §2.6).
#
# When a tool node parks for approval the superstep loop emits its ``enter``
# but SKIPS the ``exit`` ("their exit lands on the resume path"). These tests
# drive a real park -> resume and assert the resume path now emits the matching
# ``exit`` so every parked-then-resumed node has a BALANCED enter/exit pair.
# ---------------------------------------------------------------------------


def _begin_tool_end_graph() -> "Graph":
    """begin -> tool(t) -> end. The tool node parks for approval on first run."""
    return Graph(
        id="g-approval-transition",
        description="begin -> tool(t) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="t", tool_id="dangerous__tool", arguments={"q": "x"}),
            _EndNode(id="end", output_template="{{ nodes.t.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="end"),
        ],
    )


async def _drain_transitions_until_yield(
    it: "AsyncIterator[StreamEvent]",
) -> tuple[list[_GraphTransitionEvent], "YieldToWorker | None"]:
    """Collect _GraphTransitionEvents until the executor parks (raises)."""
    transitions: list[_GraphTransitionEvent] = []
    try:
        async for ev in it:
            if isinstance(ev, _GraphTransitionEvent):
                transitions.append(ev)
    except YieldToWorker as exc:
        return transitions, exc
    return transitions, None


async def _drain_transitions(
    it: "AsyncIterator[StreamEvent]",
) -> list[_GraphTransitionEvent]:
    return [ev async for ev in it if isinstance(ev, _GraphTransitionEvent)]


@pytest.mark.asyncio
async def test_resume_emits_balanced_exit_on_approve() -> None:
    """A tool node parked-for-approval then APPROVED emits a balanced
    enter/exit pair: enter on the first (parking) run, exit on the resume
    path with status='completed' (Spec B §2.6)."""
    graph = _begin_tool_end_graph()
    yielded_obj = Yielded(
        tool_name="_approval", event_key="tool_approval:sid:tc-approve",
    )

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(yielded_obj, tool_call_id="tc-approve")

    async def resume_dispatcher(node, arguments, bypass_approval=False):
        return ToolResultPart(id="tc-approve", output="approved-output")

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(
        GraphNodeMessage
    )
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

    pre, raised = await _drain_transitions_until_yield(executor.invoke([]))
    assert raised is not None
    # The parking run emitted t's enter but NOT its exit.
    t_pre = [(t.phase, t.status) for t in pre if t.node_id == "t"]
    assert t_pre == [("enter", None)], t_pre

    payload = executor.snapshot_state()
    executor2 = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=resume_dispatcher,
    )
    post = await _drain_transitions(executor2.resume_from_checkpoint(payload))

    # The resume path emits t's deferred exit with status='completed'.
    t_all = [(t.phase, t.status) for t in pre + post if t.node_id == "t"]
    assert t_all == [("enter", None), ("exit", "completed")], t_all
    # Balanced: exactly one enter and one exit for the parked-then-resumed node.
    assert sum(1 for p, _ in t_all if p == "enter") == 1
    assert sum(1 for p, _ in t_all if p == "exit") == 1


@pytest.mark.asyncio
async def test_resume_emits_balanced_exit_on_reject() -> None:
    """A tool node parked-for-approval then REJECTED emits a balanced
    enter/exit pair: enter on the first (parking) run, exit on the resume
    path with status='failed' (Spec B §2.6 / §4.8)."""
    graph = _begin_tool_end_graph()
    yielded_obj = Yielded(
        tool_name="_approval", event_key="tool_approval:sid:tc-reject",
    )

    async def first_dispatcher(node, arguments):
        raise YieldToWorker(yielded_obj, tool_call_id="tc-reject")

    async def reject_dispatcher(node, arguments, bypass_approval=False):
        raise _ToolApprovalRejected("operator rejected", tool_call_id="tc-reject")

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(
        GraphNodeMessage
    )
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

    pre, raised = await _drain_transitions_until_yield(executor.invoke([]))
    assert raised is not None
    t_pre = [(t.phase, t.status) for t in pre if t.node_id == "t"]
    assert t_pre == [("enter", None)], t_pre

    payload = executor.snapshot_state()
    executor2 = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        tool_dispatcher=reject_dispatcher,
    )
    post = await _drain_transitions(executor2.resume_from_checkpoint(payload))

    # The resume path emits t's deferred exit with status='failed'.
    t_all = [(t.phase, t.status) for t in pre + post if t.node_id == "t"]
    assert t_all == [("enter", None), ("exit", "failed")], t_all
    assert sum(1 for p, _ in t_all if p == "enter") == 1
    assert sum(1 for p, _ in t_all if p == "exit") == 1


def test_transition_record_maps_to_graph_transition_tap_event() -> None:
    """A ``graph_transition`` record maps 1:1 onto a tap event whose
    ``class`` is ``graph_transition``."""
    ev = _GraphTransitionEvent(
        node_id="n1", node_kind="agent", phase="exit", status="completed",
    )
    rec = translate_stream_event(ev, _CoalesceState())  # type: ignore[arg-type]
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.GRAPH_TRANSITION

    rec = rec.model_copy(update={"seq": 7})
    tap = record_to_tap_event(
        rec,
        workspace_id="ws-1",
        session_id="sess-1",
        agent_id=None,
        graph_id="graph-1",
        cursor="cur-1",
    )
    assert tap.class_ == TapEventClass.GRAPH_TRANSITION
    assert tap.payload == {
        "node_id": "n1",
        "node_kind": "agent",
        "phase": "exit",
        "status": "completed",
    }
    assert tap.seq == 7
