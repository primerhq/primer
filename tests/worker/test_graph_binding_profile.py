"""A graph session's binding ``profile_id`` is the run's model profile for every agent node that names none (lead ruling 2 of ticket 01a16d48).

``GraphSessionBinding.profile_id`` is described as an override for the run, mirroring ``AgentSessionBinding.profile_id``, but the graph builder never
read it: only a node's own ``profile_id`` reached the resolver. These run a graph through the worker's own builder (``pool._build_executor``, the
path every graph turn and every graph resume takes) and record the profile each node's model call was resolved with.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from primer.model.agent import Agent
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _GraphNodeRef, _StaticEdge
from primer.model.model_profile import ModelProfileConfig
from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model_profile import ResolvedModel
from tests.worker.test_executor_builders_instructions import _agent, _AgentSession, _AgentStore, _pool, _state_repo, _Workspace


class _Holder(_AgentSession):
    """The graph session's holder: the run ends it when the graph ends."""

    async def set_status(self, status, *, ended_reason=None) -> None:
        self.ended = (status, ended_reason)


class _Rows:
    def __init__(self, rows: dict[str, Any]) -> None:
        self._rows = rows

    async def get(self, row_id: str):
        return self._rows.get(row_id)


class _Storage:
    """The agent row, the stored graphs a ``graph`` node reads, and no row of anything else (the builder asks for the external-tool-call table,
    which a run with no external tools never reads)."""

    def __init__(self, agent: Agent, graphs: dict[str, Graph]) -> None:
        self._agents = _AgentStore(agent)
        self._graphs = _Rows(graphs)

    def get_storage(self, model):
        if model is Agent:
            return self._agents
        return self._graphs if model is Graph else _Rows({})


class _Scripted:
    """A model that answers ``ok`` and is shared by every node, so the order of the recorded resolutions is the order the nodes ran."""

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any):
        async def _events() -> AsyncIterator[StreamEvent]:
            yield TextDelta(text="ok", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return _events()


def _graph() -> Graph:
    """begin -> plain (no profile) -> pinned (its own profile) -> end."""
    return Graph(
        id="g-profiles", description="two agent nodes, one with a profile of its own",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="plain", agent_id="ag-1", input_template="go"),
            _AgentNodeRef(id="pinned", agent_id="ag-1", input_template="go", profile_id="prov-1--node"),
            _EndNode(id="end", output_template="{{ nodes.pinned.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="plain"),
            _StaticEdge(from_node="plain", to_node="pinned"),
            _StaticEdge(from_node="pinned", to_node="end"),
        ],
    )


def _row(graph: Graph, *, profile_id: str | None) -> WorkspaceSession:
    return WorkspaceSession(
        id="s-g", workspace_id="ws-1", binding=GraphSessionBinding(graph_id=graph.id, graph_snapshot=graph, profile_id=profile_id),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), turn_no=0,
    )


async def _run(monkeypatch, tmp_path: Path, graph: Graph, *, profile_id: str | None, graphs: dict[str, Graph] | None = None) -> list[str | None]:
    """Run ``graph`` in a graph session bound with ``profile_id``; return the override each model call was resolved with, in order."""
    pool = _pool(monkeypatch)
    asked: list[str | None] = []
    llm = _Scripted()

    async def _resolve_llm(agent, override=None):
        asked.append(override)
        chosen = override or agent.model.profile_id
        return llm, ResolvedModel(
            profile_id=chosen, provider_id="prov-1", model_name="m-1", context_length=8000, config=ModelProfileConfig(),
        )

    monkeypatch.setattr(pool, "_resolve_llm", _resolve_llm)
    monkeypatch.setattr(pool, "_storage", _Storage(_agent(), graphs or {}))
    ws = _Workspace("s-g", state_repo=await _state_repo(tmp_path))
    ws._session = _Holder("s-g")
    driver = await pool._build_executor(_row(graph, profile_id=profile_id), ws)
    await driver.invoke([])
    assert driver.last_done_reason == "graph_ended", "the run must complete for the recorded resolutions to be the whole run"
    return asked


async def test_a_profile_less_node_runs_on_the_binding_profile_and_a_node_with_its_own_keeps_it(monkeypatch, tmp_path):
    asked = await _run(monkeypatch, tmp_path, _graph(), profile_id="prov-1--run")

    assert asked == ["prov-1--run", "prov-1--node"]


async def test_without_a_binding_profile_a_profile_less_node_runs_on_its_agent_default(monkeypatch, tmp_path):
    asked = await _run(monkeypatch, tmp_path, _graph(), profile_id=None)

    assert asked == [None, "prov-1--node"], "no override reaches the resolver, which then uses the agent's own profile"


async def test_the_binding_profile_reaches_the_nodes_of_a_subgraph(monkeypatch, tmp_path):
    """A ``graph`` node's child runs in the same run, so its profile-less agent nodes take the binding's profile too."""
    child = Graph(
        id="g-child", description="one agent node",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="inner", agent_id="ag-1", input_template="go"), _EndNode(id="end")],
        edges=[_StaticEdge(from_node="begin", to_node="inner"), _StaticEdge(from_node="inner", to_node="end")],
    )
    parent = Graph(
        id="g-parent", description="a graph node",
        nodes=[_BeginNode(id="begin"), _GraphNodeRef(id="sub", graph_id="g-child", input_template="go"), _EndNode(id="end")],
        edges=[_StaticEdge(from_node="begin", to_node="sub"), _StaticEdge(from_node="sub", to_node="end")],
    )

    asked = await _run(monkeypatch, tmp_path, parent, profile_id="prov-1--run", graphs={"g-child": child})

    assert asked == ["prov-1--run"]
