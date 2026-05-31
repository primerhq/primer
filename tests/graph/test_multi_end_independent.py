"""Spec B §2.4 — Multi-End independent termination.

Spec A's rule was: "first End reached terminates the graph; lex-smallest
wins on tie". Spec B removes that — the executor's outer loop runs
until the ready set is empty AND no nodes are in-flight. Each End fires
independently when reached, producing its own ``_GraphEndOutputEvent``
(and the session translator's ``assistant_token`` record).

This file pins the NEW semantic end-to-end through the workspace
executor and per-End ``_GraphEndOutputEvent`` emission.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphEndOutputEvent
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    StreamEvent,
    TextDelta,
)
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.workspace_session import SessionMessageKind, SessionStatus
from primer.session.persistence import (
    WorkspaceMessageWriter,
    _CoalesceState,
    translate_stream_event,
)
from primer.workspace.local.state import LocalStateRepo as StateRepo


class _FakeLLM:
    """Stub LLM. End-only graphs never call it; for graphs with an Agent
    node, every ``stream()`` call replays the same scripted sequence
    (TextDelta + Done) so the executor sees a normal completion."""

    def __init__(self, *, script: list[StreamEvent] | None = None) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls.append(kwargs)
        script = self._script

        async def _replay() -> AsyncIterator[StreamEvent]:
            if not script:
                if False:
                    yield  # pragma: no cover
                return
            for ev in script:
                yield ev
        return _replay()


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self.buffers: dict[str, bytearray] = {}

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.buffers.setdefault(session_id, bytearray()).extend(line)

    def lines(self, session_id: str) -> list[str]:
        raw = bytes(self.buffers.get(session_id, b""))
        return [s for s in raw.decode("utf-8").split("\n") if s]


async def _make_state_repo(tmp_path: Path) -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-test")
    await repo.initialize()
    return repo


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


async def _run_through_writer(
    *,
    graph: Graph,
    tmp_path: Path,
    session_id: str,
    graph_input: Any = None,
    llm: _FakeLLM | None = None,
) -> tuple[list[StreamEvent], list[dict], WorkspaceGraphExecutor]:
    repo = await _make_state_repo(tmp_path)
    fake_llm = llm if llm is not None else _FakeLLM()

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
        return (fake_llm, _model())

    kwargs: dict[str, Any] = dict(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id=session_id,
    )
    if graph_input is not None:
        kwargs["graph_input"] = graph_input
    executor = WorkspaceGraphExecutor(**kwargs)

    fake_io = _FakeWorkspaceIO()
    writer = WorkspaceMessageWriter(
        workspace_io=fake_io,
        session_id=session_id,
    )
    coalesce = _CoalesceState()
    events: list[StreamEvent] = []
    async for ev in executor.invoke([]):
        events.append(ev)
        rec = translate_stream_event(ev, coalesce)
        if rec is None:
            continue
        recs = rec if isinstance(rec, list) else [rec]
        for r in recs:
            await writer.append(r)
    await writer.flush()

    persisted = [json.loads(line) for line in fake_io.lines(session_id)]
    return events, persisted, executor


# ===========================================================================
# Multi-End parallel — every End fires; graph terminates 'completed'
# ===========================================================================


@pytest.mark.asyncio
async def test_multi_end_all_fire_independently(tmp_path: Path) -> None:
    """Spec B §2.4 — when multiple Ends become ready in the same
    superstep, each fires independently. Build ``Begin -> {end_a, end_b,
    end_c}`` and assert THREE ``_GraphEndOutputEvent``s are emitted,
    each carrying its own rendered text. The graph terminates
    ``completed`` once the ready set drains; no End suppresses another."""
    graph = Graph(
        id="g-multi-end-indep",
        description="Begin -> {end_a, end_b, end_c} parallel",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(id="end_a", output_template="A-text"),
            _EndNode(id="end_b", output_template="B-text"),
            _EndNode(id="end_c", output_template="C-text"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="end_a"),
            _StaticEdge(from_node="begin", to_node="end_b"),
            _StaticEdge(from_node="begin", to_node="end_c"),
        ],
    )
    events, persisted, _executor = await _run_through_writer(
        graph=graph,
        tmp_path=tmp_path,
        session_id="sid-multi-end-indep",
    )

    end_events = [e for e in events if isinstance(e, _GraphEndOutputEvent)]
    by_id = {e.end_node_id: e for e in end_events}
    assert set(by_id.keys()) == {"end_a", "end_b", "end_c"}, (
        f"expected every End to fire independently; got {sorted(by_id.keys())!r}"
    )
    assert by_id["end_a"].text == "A-text"
    assert by_id["end_b"].text == "B-text"
    assert by_id["end_c"].text == "C-text"

    # Each End's output_template should land as its own assistant_token
    # record on the session log (one per End, distinguished by
    # ``payload.end_node_id``).
    end_records = [
        r for r in persisted
        if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN.value
        and r["payload"].get("end_node_id") is not None
    ]
    end_record_ids = {r["payload"]["end_node_id"] for r in end_records}
    assert end_record_ids == {"end_a", "end_b", "end_c"}


@pytest.mark.asyncio
async def test_multi_end_session_completes_cleanly(tmp_path: Path) -> None:
    """The session-level termination status is ``completed`` (not
    ``failed``) even though multiple Ends fired. The ended_reason is set
    once when the ready set drains, not per-End."""
    graph = Graph(
        id="g-multi-end-session",
        description="Begin -> {end_x, end_y}",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(id="end_x", output_template="X"),
            _EndNode(id="end_y", output_template="Y"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="end_x"),
            _StaticEdge(from_node="begin", to_node="end_y"),
        ],
    )
    _events, _persisted, executor = await _run_through_writer(
        graph=graph,
        tmp_path=tmp_path,
        session_id="sid-multi-end-session",
    )

    # Look up the persisted graph-state payload and confirm it ended
    # cleanly. The workspace executor's _save_state writes a state.json
    # blob with the final ended_reason after the outer loop drains.
    state = await executor.load_state()
    assert state is not None
    assert state["status"] == SessionStatus.ENDED.value
    assert state["ended_reason"] == "completed"


# ===========================================================================
# Multi-End sequential — Ends reached at different supersteps still all fire
# ===========================================================================


@pytest.mark.asyncio
async def test_multi_end_across_supersteps_all_fire(tmp_path: Path) -> None:
    """Two Ends reached in DIFFERENT supersteps both fire. Build
    ``Begin -> {end_early, agent_mid -> end_late}``: ``end_early``
    becomes ready in superstep 1 alongside ``agent_mid``; ``end_late``
    only becomes ready in superstep 2. Both Ends should produce their
    own ``_GraphEndOutputEvent`` — the early one must NOT short-circuit
    the rest of the graph (Spec A behaviour) and the late one runs to
    completion."""
    graph = Graph(
        id="g-multi-end-cross-superstep",
        description="Begin -> {end_early, agent_mid -> end_late}",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(id="end_early", output_template="EARLY"),
            _AgentNodeRef(id="agent_mid", agent_id="agent-x"),
            _EndNode(id="end_late", output_template="LATE"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="end_early"),
            _StaticEdge(from_node="begin", to_node="agent_mid"),
            _StaticEdge(from_node="agent_mid", to_node="end_late"),
        ],
    )
    llm = _FakeLLM(
        script=[
            TextDelta(text="ok", index=0),
            Done(stop_reason="stop", raw_reason="stop"),
        ]
    )
    events, _persisted, _executor = await _run_through_writer(
        graph=graph,
        tmp_path=tmp_path,
        session_id="sid-cross-superstep",
        llm=llm,
    )

    end_events = [e for e in events if isinstance(e, _GraphEndOutputEvent)]
    by_id = {e.end_node_id: e.text for e in end_events}
    assert by_id == {
        "end_early": "EARLY",
        "end_late": "LATE",
    }, (
        "expected every End reached (across BOTH supersteps) to fire; "
        f"got {by_id!r}"
    )
