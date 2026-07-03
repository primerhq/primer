"""ExecutionContext (`ctx`) is threaded into a real workspace graph run.

Covers: the workspace executor builds ``ctx`` with real ids; a subgraph child
executor gets its OWN nested scope; an End ``output_template`` that references
``{{ ctx.artifact_dir }}`` renders successfully end-to-end (proven by the run
reaching ``completed`` under StrictUndefined semantics); and the per-workflow
artifact directory is created on disk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Message, StreamEvent
from primer.model.graph import (
    Graph,
    _BeginNode,
    _EndNode,
    _GraphNodeRef,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.workspace.local.state import LocalStateRepo as StateRepo


class _FakeLLM:
    """Minimal LLM stub — Begin/End graphs never invoke it."""

    async def list_models(self) -> list[str]:
        return ["m"]

    def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        async def _empty() -> AsyncIterator[StreamEvent]:
            if False:
                yield  # pragma: no cover

        return _empty()


class _FakeWorkspaceSession:
    """AgentSession-shaped double exposing the ids build_execution_context reads."""

    workspace_id = "ws-fake"
    session_id = "sess-fake"
    agent_id = "agent-fake"
    system_prompt_fragment = "<<WORKSPACE FRAGMENT>>"

    def __init__(self) -> None:
        self.workspace_tools: list = []


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


async def _make_state_repo(tmp_path: Path) -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-fake")
    await repo.initialize()
    return repo


async def _agent_resolver(agent_id: str) -> Agent:
    return _agent(agent_id)


async def _llm_resolver(_a: Agent) -> tuple[_FakeLLM, LLMModel]:
    return (_FakeLLM(), _model())


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


def _begin_end_graph(graph_id: str, *, output_template: str = "done") -> Graph:
    return Graph(
        id=graph_id,
        description="Begin -> End",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(id="end", output_template=output_template),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )


@pytest.mark.asyncio
async def test_workspace_executor_context_has_real_ids(tmp_path: Path) -> None:
    repo = await _make_state_repo(tmp_path)
    executor = WorkspaceGraphExecutor(
        graph=_begin_end_graph("g-ctx-top"),
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-ctx-top",
        workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
    )
    ctx = executor._execution_context
    assert ctx.surface == "workspace"
    assert ctx.workspace_id == "ws-fake"
    assert ctx.session_id == "gsid-ctx-top"
    assert ctx.graph_id == "g-ctx-top"
    assert ctx.artifact_dir == "artifacts/gsid-ctx-top"


@pytest.mark.asyncio
async def test_subgraph_child_context_is_nested(tmp_path: Path) -> None:
    repo = await _make_state_repo(tmp_path)
    parent = WorkspaceGraphExecutor(
        graph=_begin_end_graph("g-ctx-parent"),
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-parent",
        workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
    )
    sub_graph = _begin_end_graph("g-ctx-sub")
    parent_node = _GraphNodeRef(id="child", graph_id="g-ctx-sub")

    child = await parent._build_sub_executor(parent_node, sub_graph)

    child_ctx = child._execution_context
    assert child_ctx.surface == "workspace"
    assert child_ctx.session_id == "gsid-parent__child"
    assert child_ctx.artifact_dir == "artifacts/gsid-parent__child"


@pytest.mark.asyncio
async def test_end_template_renders_ctx_and_artifact_dir_created(
    tmp_path: Path,
) -> None:
    # End template references ctx.artifact_dir. Under StrictUndefined, a missing
    # ctx would terminate the graph `failed` (template_error); reaching
    # `completed` proves ctx.artifact_dir rendered successfully.
    graph = _begin_end_graph("g-ctx-e2e", output_template="{{ ctx.artifact_dir }}")
    repo = await _make_state_repo(tmp_path)
    executor = WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=_agent_resolver,
        llm_resolver=_llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-ctx-e2e",
        workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
        graph_input="seed",
    )
    await _drain(executor.invoke([]))

    state = await executor.load_state()
    assert state is not None
    assert state["status"] == "ended"
    assert state["ended_reason"] == "completed"
    # Best-effort artifact dir created at the workspace root (state repo parent).
    assert (tmp_path / "artifacts" / "gsid-ctx-e2e").is_dir()
