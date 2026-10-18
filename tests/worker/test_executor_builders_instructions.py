"""Every executor the worker builds loads the workspace's agents.md as it builds (pivot P2, Task 8).

The worker builds one agent executor per turn, one graph executor per graph run and one child executor per graph an
agent invokes, and each reads the file at that moment: an edit between two turns reaches the second one, and a file
nobody can read never fails a build.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import NotFoundError
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge
from primer.model.model_profile import ModelProfileConfig
from primer.model.scheduler import WorkerConfig
from primer.model.workspace import FileEntry
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model_profile import ResolvedModel
from primer.worker.pool import WorkerPool
from primer.workspace.instructions import workspace_instructions_part
from primer.workspace.local.state import LocalStateRepo

ROOT = Path(__file__).resolve().parents[2]
FRAGMENT = "[fake workspace prompt]"


def test_every_builder_loads_the_instructions():
    """A tripwire for a new builder that forgets the file; the behaviour tests below are the real check."""
    src = (ROOT / "primer/worker/executor_builders.py").read_text()
    for fn in ("async def build_agent_executor", "async def build_graph_executor", "async def build_child_executor"):
        body = src[src.index(fn):]
        body = body[:body.index("\n    return ")]
        assert "await load_workspace_instructions(workspace)" in body, fn


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _AgentSession:
    """Just enough AgentSession for the builders (the executors read the fragment and the ids)."""

    def __init__(self, sid: str) -> None:
        self.session_id = sid
        self.workspace_id = "ws-1"
        self.agent_id = "ag-1"
        self.workspace_tools: list = []
        self.system_prompt_fragment = FRAGMENT


class _Workspace:
    """A workspace stub: the holder session, a state repo for graph runs, and a ``read_file`` over a dict."""

    id = "ws-1"

    def __init__(self, sid: str, *, files=None, state_repo=None, holder: bool = True, error: Exception | None = None):
        self._session = _AgentSession(sid)
        self._holder = holder
        self.files: dict[str, bytes] = dict(files or {})
        self.state_repo = state_repo
        self.error = error

    async def get_session(self, session_id):
        return self._session if self._holder and session_id == self._session.session_id else None

    async def file_info(self, path: str) -> FileEntry:
        if self.error is not None:
            raise self.error
        if path not in self.files:
            raise NotFoundError(f"{path!r} not found")
        return FileEntry(path=path, kind="file", size_bytes=len(self.files[path]), modified_at=datetime.now(timezone.utc))

    async def read_file(self, path: str) -> bytes:
        if self.error is not None:
            raise self.error
        if path not in self.files:
            raise NotFoundError(f"{path!r} not found")
        return self.files[path]


class _AgentStore:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def get(self, agent_id: str):
        return self._agent if agent_id == self._agent.id else None


class _Storage:
    def __init__(self, agent: Agent) -> None:
        self._store = _AgentStore(agent)

    def get_storage(self, model):
        assert model is Agent
        return self._store


def _agent() -> Agent:
    return Agent(
        id="ag-1", description="test agent", model=AgentModel(profile_id="prov-1--m-1"), tools=[],
        system_prompt=["sys"],
    )


def _pool(monkeypatch, *, agent: Agent | None = None) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                 # type: ignore[arg-type]
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )

    async def _resolve_llm(_agent, _override=None):
        return object(), ResolvedModel(
            profile_id="prov-1--m-1", provider_id="prov-1", model_name="m-1", context_length=8000,
            config=ModelProfileConfig(),
        )

    monkeypatch.setattr(pool, "_resolve_llm", _resolve_llm)
    if agent is not None:
        monkeypatch.setattr(pool, "_storage", _Storage(agent))
    return pool


def _agent_row(sid: str, agent: Agent) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1", agent_snapshot=agent),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), turn_no=0,
    )


def _graph() -> Graph:
    return Graph(
        id="g-1", description="begin -> agent -> end",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="work", agent_id="ag-1"), _EndNode(id="end")],
        edges=[_StaticEdge(from_node="begin", to_node="work"), _StaticEdge(from_node="work", to_node="end")],
    )


def _graph_row(sid: str) -> WorkspaceSession:
    graph = _graph()
    return WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=GraphSessionBinding(graph_id=graph.id, graph_snapshot=graph),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), turn_no=0,
    )


async def _state_repo(tmp_path: Path) -> LocalStateRepo:
    repo = LocalStateRepo(tmp_path / "state", workspace_id="ws-1")
    await repo.initialize()
    return repo


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# The agent builder: one executor per turn
# ---------------------------------------------------------------------------


async def test_the_agent_builder_puts_the_file_in_the_prompt(monkeypatch):
    agent = _agent()
    pool = _pool(monkeypatch)
    ws = _Workspace("s-1", files={"agents.md": b"rules"})

    driver = await pool._build_executor(_agent_row("s-1", agent), ws)

    assert driver._executor._agent.system_prompt == ["sys", FRAGMENT, workspace_instructions_part("rules")]
    assert agent.system_prompt == ["sys"], "the session's frozen agent is not changed"


async def test_the_agent_builder_reads_the_file_afresh_for_every_turn(monkeypatch):
    agent = _agent()
    pool = _pool(monkeypatch)
    ws = _Workspace("s-1", files={"agents.md": b"one"})

    first = await pool._build_executor(_agent_row("s-1", agent), ws)
    ws.files["agents.md"] = b"two"
    second = await pool._build_executor(_agent_row("s-1", agent), ws)
    del ws.files["agents.md"]
    third = await pool._build_executor(_agent_row("s-1", agent), ws)

    assert first._executor._agent.system_prompt[-1] == workspace_instructions_part("one")
    assert second._executor._agent.system_prompt[-1] == workspace_instructions_part("two")
    assert third._executor._agent.system_prompt == ["sys", FRAGMENT], "a deleted file is gone from the next turn"


async def test_the_agent_builder_uses_the_upper_case_file_when_that_is_all_there_is(monkeypatch):
    pool = _pool(monkeypatch)
    ws = _Workspace("s-1", files={"AGENTS.md": b"UP"})

    driver = await pool._build_executor(_agent_row("s-1", _agent()), ws)

    assert driver._executor._agent.system_prompt[-1] == workspace_instructions_part("UP")


async def test_the_agent_builder_without_the_file_builds_the_prompt_as_before(monkeypatch, caplog):
    pool = _pool(monkeypatch)

    driver = await pool._build_executor(_agent_row("s-1", _agent()), _Workspace("s-1"))

    assert driver._executor._agent.system_prompt == ["sys", FRAGMENT]
    assert _warnings(caplog) == []


async def test_an_unreadable_file_does_not_fail_the_build(monkeypatch, caplog):
    pool = _pool(monkeypatch)
    ws = _Workspace("s-1", error=OSError("disk gone"))

    driver = await pool._build_executor(_agent_row("s-1", _agent()), ws)

    assert driver._executor._agent.system_prompt == ["sys", FRAGMENT], "the turn runs without the file"
    assert len([r for r in _warnings(caplog) if r.getMessage().startswith("workspace instructions: agents.md in ws-1")]) == 1


# ---------------------------------------------------------------------------
# The graph builder: every node agent of a graph run
# ---------------------------------------------------------------------------


async def test_the_graph_builder_hands_the_file_to_every_node_agent(monkeypatch, tmp_path):
    pool = _pool(monkeypatch, agent=_agent())
    ws = _Workspace("s-g", files={"agents.md": b"rules"}, state_repo=await _state_repo(tmp_path))

    driver = await pool._build_executor(_graph_row("s-g"), ws)

    node_agent = await driver._executor._agent_resolver("ag-1")
    assert node_agent.system_prompt == ["sys", FRAGMENT, workspace_instructions_part("rules")]


async def test_the_graph_builder_reads_the_file_afresh_for_every_run(monkeypatch, tmp_path):
    pool = _pool(monkeypatch, agent=_agent())
    ws = _Workspace("s-g", files={"agents.md": b"one"}, state_repo=await _state_repo(tmp_path))

    first = await pool._build_executor(_graph_row("s-g"), ws)
    ws.files["agents.md"] = b"two"
    second = await pool._build_executor(_graph_row("s-g"), ws)

    assert (await first._executor._agent_resolver("ag-1")).system_prompt[-1] == workspace_instructions_part("one")
    assert (await second._executor._agent_resolver("ag-1")).system_prompt[-1] == workspace_instructions_part("two")


async def test_a_graph_session_with_no_holder_still_carries_the_file(monkeypatch, tmp_path):
    """A graph session created before holder sessions existed has no workspace session; its workspace's file still
    applies to its agents."""
    pool = _pool(monkeypatch, agent=_agent())
    ws = _Workspace("s-g", files={"agents.md": b"rules"}, state_repo=await _state_repo(tmp_path), holder=False)

    driver = await pool._build_executor(_graph_row("s-g"), ws)

    node_agent = await driver._executor._agent_resolver("ag-1")
    assert node_agent.system_prompt == ["sys", workspace_instructions_part("rules")]


async def test_the_graph_builder_without_the_file_leaves_the_node_prompts_as_before(monkeypatch, tmp_path):
    pool = _pool(monkeypatch, agent=_agent())
    ws = _Workspace("s-g", state_repo=await _state_repo(tmp_path))

    driver = await pool._build_executor(_graph_row("s-g"), ws)

    assert (await driver._executor._agent_resolver("ag-1")).system_prompt == ["sys", FRAGMENT]


# ---------------------------------------------------------------------------
# The child executor of a graph an agent invokes
# ---------------------------------------------------------------------------


async def test_the_child_executor_of_an_invoked_graph_carries_the_file(monkeypatch, tmp_path):
    pool = _pool(monkeypatch, agent=_agent())
    ws = _Workspace("s-1", files={"agents.md": b"one"}, state_repo=await _state_repo(tmp_path))
    services = pool._build_graph_invocation_services(
        workspace=ws, workspace_session=ws._session, graph_session_id="s-1", initiated_by=None,
    )
    assert services is not None

    first = await services.build_child_executor(graph=_graph(), gsid="s-1__first")
    ws.files["agents.md"] = b"two"
    second = await services.build_child_executor(graph=_graph(), gsid="s-1__second")

    assert (await first._agent_resolver("ag-1")).system_prompt == ["sys", FRAGMENT, workspace_instructions_part("one")]
    assert (await second._agent_resolver("ag-1")).system_prompt == ["sys", FRAGMENT, workspace_instructions_part("two")], (
        "each graph run reads the file as it is then, not as it was when the turn started"
    )


async def test_an_unreadable_file_does_not_fail_a_child_executor_build(monkeypatch, tmp_path, caplog):
    pool = _pool(monkeypatch, agent=_agent())
    ws = _Workspace("s-1", error=OSError("disk gone"), state_repo=await _state_repo(tmp_path))
    services = pool._build_graph_invocation_services(
        workspace=ws, workspace_session=ws._session, graph_session_id="s-1", initiated_by=None,
    )

    child = await services.build_child_executor(graph=_graph(), gsid="s-1__child")

    assert (await child._agent_resolver("ag-1")).system_prompt == ["sys", FRAGMENT]
    assert len(_warnings(caplog)) == 1
