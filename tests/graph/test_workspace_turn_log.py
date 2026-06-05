"""Verifies WorkspaceGraphExecutor emits per-node + graph-level
turn-log events to the right files in .state/graphs/<gsid>/...
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

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
from primer.model.provider import LLMModel
from primer.model.turn_log import TurnLogKind
from primer.workspace.local.state import LocalStateRepo as StateRepo


class _FakeLLM:
    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._cursor = 0

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._impl(self._scripts[idx])

    async def _impl(self, events):
        for ev in events:
            yield ev


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _make_repo(tmp_path: Path) -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-test")
    await repo.initialize()
    return repo


async def _build_executor(
    *, graph: Graph, llm, repo: StateRepo, gsid: str, agents,
) -> WorkspaceGraphExecutor:
    async def agent_resolver(aid):
        return agents[aid]

    async def llm_resolver(agent):
        return (llm, _model())

    return WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id=gsid,
    )


async def _drain(it: AsyncIterator[StreamEvent]):
    async for _ in it:
        pass


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_per_node_started_completed_lands_in_node_file(tmp_path: Path):
    graph = Graph(
        id="g-ws",
        description="A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )
    llm = _FakeLLM(scripts=[[
        TextDelta(text="hello", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]])
    repo = await _make_repo(tmp_path)
    executor = await _build_executor(
        graph=graph, llm=llm, repo=repo, gsid="gsid-A",
        agents={"x": _agent("x")},
    )
    await _drain(executor.invoke([]))

    node_log = (
        executor.state_root / "nodes" / "A" / "turns.jsonl"
    )
    rows = _read_jsonl(node_log)
    kinds = [r["kind"] for r in rows]
    assert TurnLogKind.STARTED.value in kinds
    assert TurnLogKind.COMPLETED.value in kinds
    started = next(r for r in rows if r["kind"] == TurnLogKind.STARTED.value)
    assert started["node_id"] == "A"
    assert started["iteration"] is not None
    assert started["superstep_id"] is not None


@pytest.mark.asyncio
async def test_node_writers_cached_across_supersteps(tmp_path: Path):
    """The per-node writer cache MUST hold the SAME writer instance
    across supersteps so a node appearing in N supersteps keeps a
    single monotonic seq stream. Without the cache, the per-superstep
    restart would reset seq=1 every iteration -- broken since_seq
    pagination AND colliding StorageTurnLogWriter ids."""
    graph = Graph(
        id="g-ws",
        description="A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )
    llm = _FakeLLM(scripts=[[
        TextDelta(text="hi", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]])
    repo = await _make_repo(tmp_path)
    executor = await _build_executor(
        graph=graph, llm=llm, repo=repo, gsid="gsid-cache",
        agents={"x": _agent("x")},
    )

    # Pre-populate the cache for node "A" with a sentinel writer, then
    # run the graph. The superstep-loop's writer lookup must reuse the
    # pre-existing entry -- never call the factory for "A".
    factory_calls: list[str] = []
    original_factory = executor._turn_log_factory

    def _tracking_factory(nid: str):
        factory_calls.append(nid)
        return original_factory(nid)

    executor._turn_log_factory = _tracking_factory  # type: ignore[assignment]
    sentinel = original_factory("A")
    executor._node_turn_logs["A"] = sentinel

    await _drain(executor.invoke([]))

    # Factory was NOT called for "A" (cache hit), but was for "begin"
    # and "exit" (cache misses). After the run, _close_turn_logs has
    # cleared the cache.
    assert "A" not in factory_calls
    assert "begin" in factory_calls or "exit" in factory_calls
    assert executor._node_turn_logs == {}


@pytest.mark.asyncio
async def test_superstep_events_land_in_graph_level_file(tmp_path: Path):
    graph = Graph(
        id="g-ws",
        description="A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )
    llm = _FakeLLM(scripts=[[
        TextDelta(text="ok", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]])
    repo = await _make_repo(tmp_path)
    executor = await _build_executor(
        graph=graph, llm=llm, repo=repo, gsid="gsid-B",
        agents={"x": _agent("x")},
    )
    await _drain(executor.invoke([]))

    graph_log = executor.state_root / "turns.jsonl"
    rows = _read_jsonl(graph_log)
    kinds = [r["kind"] for r in rows]
    assert TurnLogKind.SUPERSTEP_STARTED.value in kinds
    assert TurnLogKind.SUPERSTEP_ENDED.value in kinds
    # At least one superstep_started + one matching superstep_ended.
    started_count = kinds.count(TurnLogKind.SUPERSTEP_STARTED.value)
    ended_count = kinds.count(TurnLogKind.SUPERSTEP_ENDED.value)
    assert started_count >= 1
    assert ended_count == started_count
    # All superstep_started events carry a non-empty ready_node_ids.
    for r in rows:
        if r["kind"] == TurnLogKind.SUPERSTEP_STARTED.value:
            assert isinstance(r["ready_node_ids"], list)
            assert len(r["ready_node_ids"]) >= 1


@pytest.mark.asyncio
async def test_node_failed_carries_problem_details(tmp_path: Path):
    """A failing agent node lands a `failed` event with structured error
    details (NetworkError -> 504 / Network Error) in the node's turn-log
    file. Verifies the BaseException path uses to_problem_details rather
    than the generic 500 wrap."""
    graph = Graph(
        id="g-fail",
        description="A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )

    class _BrokenLLM:
        async def list_models(self):
            return ["m"]

        def stream(self, *, model, messages, **kwargs):
            return self._impl()

        async def _impl(self):
            from primer.model.except_ import NetworkError
            raise NetworkError("provider down")
            yield  # pragma: no cover

    repo = await _make_repo(tmp_path)
    executor = await _build_executor(
        graph=graph, llm=_BrokenLLM(), repo=repo, gsid="gsid-C",
        agents={"x": _agent("x")},
    )
    await _drain(executor.invoke([]))

    node_log = executor.state_root / "nodes" / "A" / "turns.jsonl"
    rows = _read_jsonl(node_log)
    failed_rows = [r for r in rows if r["kind"] == TurnLogKind.FAILED.value]
    assert len(failed_rows) >= 1
    f = failed_rows[0]
    # The error envelope is a ProblemDetails-shaped dict with the real
    # NetworkError mapping (not the generic graph-node-failed wrap).
    assert isinstance(f["error"], dict)
    assert f["error"]["status"] == 504, f["error"]
    assert f["error"]["title"] == "Network Error"
    assert "provider down" in f["error"]["detail"]
    assert f["error"]["type"] == "/errors/network-error"
    assert f["error"]["extensions"]["exception_class"] == "NetworkError"
