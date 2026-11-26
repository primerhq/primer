"""Review 2026-11-23 finding 6.2: the superstep-boundary resume shapes beside the main repro.

The main repro (``test_review_6_2_reclaim_reruns_from_begin.py``) covers the cut after a node settled. These
cover the remaining shapes the resume rule must handle, against a STUB state repo: the git-backed
``LocalStateRepo`` measures about 6 s per run (the audit's figure), so these tests use a file-backed stub
that exposes exactly the interface the executor calls (``initialize`` / ``path`` / ``commit_arbitrary`` /
``read_state_file``); the turn-log writers do direct file I/O under ``path`` and work unchanged.

* a crash during the first node re-runs only that node - never Begin - and the re-run node's prompt does
  not carry its cut turn's own answer back in;
* a completed run reclaimed does nothing more and still reports its persisted terminal outcome (the
  worker's post-turn status mapper reads ``last_done_reason`` off the executor);
* a failed run reclaimed does the same, reporting failed;
* a non-session-owning (child) executor's reclaimed completed run re-emits its terminal End output so a
  re-run parent node re-derives the same NodeOutput as the original run.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphEndOutputEvent, _GraphTransitionEvent
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    _GraphNodeEvent,
)
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel


class _StubStateRepo:
    """File-backed stand-in for LocalStateRepo: same interface, no git.

    ``commit_arbitrary`` writes the files under ``path`` (the same
    ``.state/``-relative layout the real repo uses); ``read_state_file``
    reads them back. The turn-log writers write directly under ``path`` too.
    """

    def __init__(self, root: Path, workspace_id: str) -> None:
        self._path = root
        self.workspace_id = workspace_id

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        self._path.mkdir(parents=True, exist_ok=True)

    async def commit_arbitrary(
        self,
        *,
        summary: str,
        files: dict[str, str | bytes] | None = None,
        delete_files: list[str] | None = None,
        trailers: dict[str, str] | None = None,
    ) -> str:
        for rel, content in (files or {}).items():
            target = self._path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            target.write_bytes(data)
        for rel in delete_files or []:
            (self._path / rel).unlink(missing_ok=True)
        return "stub-sha"

    async def read_state_file(self, path: str) -> bytes | None:
        target = self._path / path
        if not target.exists():
            return None
        return target.read_bytes()


class _RecordingLLM:
    """One fake LLM per node: every execution of its node appends one call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text="done", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


class _RaisingLLM:
    """A node agent whose turn always fails."""

    def __init__(self) -> None:
        self.attempts = 0

    async def list_models(self):
        return ["m"]

    def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.attempts += 1
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this a generator


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="test-profile",
        provider_id="test-provider",
        model_name="m",
        context_length=128_000,
        config=ModelProfileConfig(),
    )


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(profile_id="p--m"),
        system_prompt=[],
    )


def _graph(nodes: list, edges: list) -> Graph:
    return Graph(
        id="g-resume",
        description="resume shape under test",
        nodes=nodes,
        edges=edges,
    )


def _linear_graph() -> Graph:
    return _graph(
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="n1", agent_id="a1"),
            _AgentNodeRef(id="n2", agent_id="a2"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="n1"),
            _StaticEdge(from_node="n1", to_node="n2"),
            _StaticEdge(from_node="n2", to_node="exit"),
        ],
    )


async def _build(
    state_repo: _StubStateRepo,
    fakes: dict[str, Any],
    graph_session_id: str,
    *,
    graph: Graph | None = None,
    owns_session_lifecycle: bool = True,
) -> WorkspaceGraphExecutor:
    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(agent: Agent):
        return (fakes[agent.id], _model())

    return WorkspaceGraphExecutor(
        graph=graph or _linear_graph(),
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=state_repo,
        graph_session_id=graph_session_id,
        owns_session_lifecycle=owns_session_lifecycle,
    )


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


def _enter_events(events: list[Any]) -> list[str]:
    """The node ids of graph-transition ENTER events, in stream order."""
    return [ev.node_id for ev in events if isinstance(ev, _GraphTransitionEvent) and ev.phase == "enter"]


def _wrapped(ev: StreamEvent) -> _GraphNodeEvent | None:
    if isinstance(ev, ExtendedEvent) and isinstance(ev.extended, _GraphNodeEvent):
        return ev.extended
    return None


@pytest.mark.asyncio
async def test_a_crash_during_the_first_node_reruns_only_that_node(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", workspace_id="ws-crash1")
    await repo.initialize()
    fakes = {"a1": _RecordingLLM()}
    graph = _graph(
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="n1", agent_id="a1"), _EndNode(id="exit")],
        edges=[_StaticEdge(from_node="begin", to_node="n1"), _StaticEdge(from_node="n1", to_node="exit")],
    )
    executor = await _build(repo, fakes, "gsid-first", graph=graph)
    gen = executor.invoke([])
    try:
        async for ev in gen:
            wrapped = _wrapped(ev)
            if wrapped is not None and wrapped.node_id == "n1" and wrapped.inner_type == "done":
                break
        else:
            raise AssertionError("the stream ended before node 1 completed")
    finally:
        await gen.aclose()

    assert len(fakes["a1"].calls) == 1

    # Reclaim: the fresh executor must resume the in-flight node, not Begin.
    reclaimed = await _build(repo, fakes, "gsid-first", graph=graph)
    events = await _drain(reclaimed.invoke([]))

    # The run re-enters at node 1: no Begin enter, node 1 enters exactly once.
    enters = _enter_events(events)
    assert "begin" not in enters, f"the reclaimed run re-ran from Begin: {enters}"
    assert enters.count("n1") == 1, f"node 1 entered {enters.count('n1')} times after the reclaim: {enters}"

    # Node 1's re-run ran once more from its own start, with its cut turn's
    # answer NOT fed back into the prompt.
    assert len(fakes["a1"].calls) == 2
    rerun_messages = fakes["a1"].calls[1]["messages"]
    assert all(m.role != "assistant" for m in rerun_messages), (
        "the re-run node's prompt carried its cut turn's own answer back in: "
        + json.dumps([m.model_dump(mode="json") for m in rerun_messages])
    )


@pytest.mark.asyncio
async def test_a_completed_run_reclaimed_does_nothing_more(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", workspace_id="ws-done")
    await repo.initialize()
    fakes = {"a1": _RecordingLLM(), "a2": _RecordingLLM()}
    first = await _build(repo, fakes, "gsid-done")
    await _drain(first.invoke([]))
    assert (len(fakes["a1"].calls), len(fakes["a2"].calls)) == (1, 1)
    assert first._last_ended_reason == "completed"

    # Reclaim: a fresh executor for the finished run does nothing more - no
    # events, no node re-runs - and still reports the persisted outcome so
    # the worker's post-turn status mapper ends the session correctly.
    reclaimed = await _build(repo, fakes, "gsid-done")
    events = await _drain(reclaimed.invoke([]))
    assert events == [], f"the reclaimed completed run still ran something: {events!r}"
    assert (len(fakes["a1"].calls), len(fakes["a2"].calls)) == (1, 1)
    assert reclaimed._last_ended_reason == "completed"
    assert reclaimed.last_done_reason == "graph_ended"


@pytest.mark.asyncio
async def test_a_failed_run_reclaimed_does_nothing_more(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", workspace_id="ws-failed")
    await repo.initialize()
    fakes = {"a1": _RaisingLLM(), "a2": _RecordingLLM()}
    first = await _build(repo, fakes, "gsid-failed")
    await _drain(first.invoke([]))
    assert first._last_ended_reason == "failed"

    # Reclaim: the failed run does nothing more and reports failed - a
    # re-run from Begin would have re-attempted node 1 (and read as
    # completed if the driver saw no terminal reason).
    reclaimed = await _build(repo, fakes, "gsid-failed")
    events = await _drain(reclaimed.invoke([]))
    assert events == [], f"the reclaimed failed run still ran something: {events!r}"
    assert fakes["a1"].attempts == 1, "the reclaimed failed run re-ran the failed node"
    assert reclaimed._last_ended_reason == "failed"
    assert reclaimed.last_done_reason == "graph_failed"


@pytest.mark.asyncio
async def test_a_non_owning_child_reclaim_rerenders_the_end_output(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", workspace_id="ws-child")
    await repo.initialize()
    fakes = {"a1": _RecordingLLM()}
    graph = _graph(
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="n1", agent_id="a1"), _EndNode(id="exit")],
        edges=[_StaticEdge(from_node="begin", to_node="n1"), _StaticEdge(from_node="n1", to_node="exit")],
    )
    first = await _build(repo, fakes, "gsid-child", graph=graph, owns_session_lifecycle=False)
    events = await _drain(first.invoke([]))
    end_events = [ev for ev in events if isinstance(ev, _GraphEndOutputEvent)]
    assert len(end_events) == 1, "the run's End node must emit exactly one end-output event"
    original = end_events[0]

    # A child executor (subgraph / invoke_graph) is not the session owner: a
    # reclaimed completed child re-emits its terminal End output so the
    # re-running parent node re-derives the same NodeOutput as before, and
    # nothing else runs.
    reclaimed = await _build(repo, fakes, "gsid-child", graph=graph, owns_session_lifecycle=False)
    again = await _drain(reclaimed.invoke([]))
    assert again == [original], f"the reclaimed child re-emitted the wrong terminal events: {again!r}"
    assert len(fakes["a1"].calls) == 1
