"""Review 2026-11-23 finding 6.2: a graph run cannot resume after a worker crash: it re-runs from Begin.

``GraphExecutor.invoke`` (``primer/graph/base.py``) always seeds ``ready = {_resolve_initial_ready_node(...)}``
(Begin), so a turn the claim loop reclaims after the worker dies re-runs the whole graph: every node executes
again, and each re-run agent node is also fed its own committed history. This test runs a linear 3-node graph
with a recording LLM per node, interrupts the turn after node 2 has settled and node 3 has entered but not
started (closing the invoke stream on n3's graph-transition "enter" event - the crash path that saves the
cut state), then re-runs the turn the way a reclaimed turn would (a fresh executor for the same graph
session, invoking the same state subtree) and asserts node 1 and node 2 executed exactly once in total and
node 3 ran once after the resume.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphTransitionEvent
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
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
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from primer.workspace.local.state import LocalStateRepo as StateRepo


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


def _graph() -> Graph:
    return Graph(
        id="g-crash",
        description="n1 -> n2 -> n3 -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="n1", agent_id="a1"),
            _AgentNodeRef(id="n2", agent_id="a2"),
            _AgentNodeRef(id="n3", agent_id="a3"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="n1"),
            _StaticEdge(from_node="n1", to_node="n2"),
            _StaticEdge(from_node="n2", to_node="n3"),
            _StaticEdge(from_node="n3", to_node="exit"),
        ],
    )


async def _build(
    state_repo: StateRepo, fakes: dict[str, _RecordingLLM], graph_session_id: str,
) -> WorkspaceGraphExecutor:
    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(agent: Agent):
        return (fakes[agent.id], _model())

    return WorkspaceGraphExecutor(
        graph=_graph(),
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=state_repo,
        graph_session_id=graph_session_id,
    )


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


@pytest.mark.asyncio
async def test_a_reclaimed_graph_run_resumes_instead_of_rerunning_from_begin(tmp_path: Path) -> None:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-crash")
    await repo.initialize()
    fakes = {"a1": _RecordingLLM(), "a2": _RecordingLLM(), "a3": _RecordingLLM()}

    # First run: the worker dies (the turn is cut) after node 2 has settled
    # and node 3 has entered its superstep but not started: cut on n3's
    # graph-transition "enter" event, which lands before the boundary save
    # and before any n3 node task exists.
    executor = await _build(repo, fakes, "gsid-crash")
    gen = executor.invoke([])
    try:
        async for ev in gen:
            if (
                isinstance(ev, _GraphTransitionEvent)
                and ev.node_id == "n3"
                and ev.phase == "enter"
            ):
                assert len(fakes["a3"].calls) == 0, "node 3 started before the crash point"
                break
        else:
            raise AssertionError("the stream ended before node 3 entered")
    finally:
        await gen.aclose()

    assert (len(fakes["a1"].calls), len(fakes["a2"].calls), len(fakes["a3"].calls)) == (1, 1, 0)

    # The reclaim: a fresh executor for the same graph session re-runs the
    # turn (the claim loop reclaims a dead turn and re-runs it) and must
    # resume from the last persisted superstep: node 3 only, with nodes 1
    # and 2 settled (their outputs restored from the persisted state).
    reclaimed = await _build(repo, fakes, "gsid-crash")
    assert await reclaimed.load_state() is not None, "the crash path persisted state.json"
    await _drain(reclaimed.invoke([]))

    # A resumable run executes only node 3: node 1 and node 2 ran exactly
    # once in total.
    assert len(fakes["a1"].calls) == 1, f"node 1 executed {len(fakes['a1'].calls)} times in total"
    assert len(fakes["a2"].calls) == 1, f"node 2 executed {len(fakes['a2'].calls)} times in total"
    assert len(fakes["a3"].calls) == 1, f"node 3 executed {len(fakes['a3'].calls)} times in total"
