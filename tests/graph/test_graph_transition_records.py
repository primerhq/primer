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

from primer.graph.base import _GraphTransitionEvent
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, StreamEvent, TextDelta
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.persistence import (
    WorkspaceMessageWriter,
    _CoalesceState,
    translate_stream_event,
)
from primer.tap.event import TapEventClass, record_to_tap_event
from primer.workspace.local.state import LocalStateRepo as StateRepo


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
