"""Every executor the worker builds carries the session row's base_prompt (Wiki First W4).

The worker builds one agent executor per turn and one graph executor per
graph run, reading the base prompt off the ROW at build time (U5, and its
R3 boundary: an invoked child graph does NOT inherit it). Because the read
happens on every build, a resume - which re-reads the row and rebuilds -
carries the field with no resume plumbing (U7).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import asyncio

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import NotFoundError
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _GraphNodeRef,
    _StaticEdge,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.scheduler import WorkerConfig
from primer.model.workspace import FileEntry, Workspace
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model_profile import ResolvedModel
from primer.worker.pool import WorkerPool
from primer.workspace.local.state import LocalStateRepo
from tests.conftest import _FakeStorageProvider

FRAGMENT = "[fake workspace prompt]"
BASE = "ALWAYS: answer the token question with the token TOKEN-42"


# ---------------------------------------------------------------------------
# Fakes (modeled on tests/worker/test_executor_builders_instructions.py, with
# a two-agent store and a base_prompt on the row)
# ---------------------------------------------------------------------------


class _AgentSession:
    """Just enough AgentSession for the builders (the executors read the fragment and the ids)."""

    def __init__(self, sid: str) -> None:
        self.session_id = sid
        self.workspace_id = "ws-1"
        self.agent_id = "ag-1"
        self.workspace_tools: list = []
        self.system_prompt_fragment = FRAGMENT
        self.base_prompt = None
        # The resume tail persists through the holder's messages lock + commit
        # (WorkspaceAgentExecutor._persist_turn); the append-only path keeps
        # that to one call the fake records.
        self.messages_lock = asyncio.Lock()
        self.committed: list[bytes] = []

    def append_only(self) -> bool:
        return True

    async def commit_state(self, *, summary: str, op: str, appends: dict) -> None:
        self.committed.append(appends.get("messages.jsonl", b""))


class _Workspace:
    """A workspace stub: the holder session, a state repo for graph runs, and a ``read_file`` over a dict."""

    id = "ws-1"

    def __init__(self, sid: str, *, state_repo=None) -> None:
        self._session = _AgentSession(sid)
        self.state_repo = state_repo

    async def get_session(self, session_id):
        return self._session if session_id == self._session.session_id else None

    async def file_info(self, path: str) -> FileEntry:
        raise NotFoundError(f"{path!r} not found")

    async def read_file(self, path: str) -> bytes:
        raise NotFoundError(f"{path!r} not found")


class _AgentStore:
    def __init__(self, agents: dict[str, Agent]) -> None:
        self._agents = agents

    async def get(self, agent_id: str):
        return self._agents.get(agent_id)


class _NoRows:
    async def get(self, _id: str):
        return None


class _Storage:
    def __init__(self, agents: dict[str, Agent]) -> None:
        self._store = _AgentStore(agents)
        self._no_rows = _NoRows()

    def get_storage(self, model):
        if model is Graph or model is Workspace:
            # The per-turn access checks ask for the live graph and the session's workspace; none is stored here, so the snapshot's access
            # decides and the workspace check has no row to protect.
            return self._no_rows
        assert model is Agent
        return self._store


def _agent(agent_id: str = "ag-1") -> Agent:
    return Agent(access=Access(public=True),
        id=agent_id, description="test agent", model=AgentModel(profile_id="prov-1--m-1"), tools=[],
        system_prompt=["sys"],
    )


def _pool(monkeypatch, *, agents: dict[str, Agent] | None = None) -> WorkerPool:
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
    if agents is not None:
        monkeypatch.setattr(pool, "_storage", _Storage(agents))
    return pool


def _agent_row(sid: str, agent: Agent, *, base_prompt: str | None = None) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=AgentSessionBinding(agent_id=agent.id, agent_snapshot=agent),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), turn_no=0,
        base_prompt=base_prompt,
    )


def _two_node_graph() -> Graph:
    return Graph(access=Access(public=True),
        id="g-1", description="begin -> a -> b -> end",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="a", agent_id="ag-1"),
            _AgentNodeRef(id="b", agent_id="ag-2"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="a"),
            _StaticEdge(from_node="a", to_node="b"),
            _StaticEdge(from_node="b", to_node="end"),
        ],
    )


def _graph_row(sid: str, *, base_prompt: str | None = None) -> WorkspaceSession:
    graph = _two_node_graph()
    return WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=GraphSessionBinding(graph_id=graph.id, graph_snapshot=graph),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), turn_no=0,
        base_prompt=base_prompt,
    )


async def _state_repo(tmp_path: Path) -> LocalStateRepo:
    repo = LocalStateRepo(tmp_path / "state", workspace_id="ws-1")
    await repo.initialize()
    return repo


# ---------------------------------------------------------------------------
# U5 -- the agent builder
# ---------------------------------------------------------------------------


async def test_the_agent_builder_puts_the_base_prompt_first(monkeypatch):
    pool = _pool(monkeypatch)
    ws = _Workspace("s-1")

    driver = await pool._build_executor(_agent_row("s-1", _agent(), base_prompt=BASE), ws)

    assert driver._executor._agent.system_prompt == [BASE, "sys", FRAGMENT]


@pytest.mark.parametrize("text", [None, ""])
async def test_a_blank_or_absent_base_prompt_builds_the_prompt_as_before(monkeypatch, text):
    pool = _pool(monkeypatch)
    ws = _Workspace("s-1")

    driver = await pool._build_executor(_agent_row("s-1", _agent(), base_prompt=text), ws)

    assert driver._executor._agent.system_prompt == ["sys", FRAGMENT]


# ---------------------------------------------------------------------------
# U5 -- the graph builder: every node agent
# ---------------------------------------------------------------------------


async def test_the_graph_builder_puts_the_base_prompt_first_for_every_node(monkeypatch, tmp_path):
    pool = _pool(monkeypatch, agents={"ag-1": _agent("ag-1"), "ag-2": _agent("ag-2")})
    ws = _Workspace("s-g", state_repo=await _state_repo(tmp_path))

    driver = await pool._build_executor(_graph_row("s-g", base_prompt=BASE), ws)

    for node_id in ("ag-1", "ag-2"):
        node_agent = await driver._executor._agent_resolver(node_id)
        assert node_agent.system_prompt == [BASE, "sys", FRAGMENT], node_id


async def test_a_subgraph_level_keeps_the_base_prompt(monkeypatch, tmp_path):
    """R5 has no per-node opt-out: the subgraph child re-wraps the RAW
    resolver, so it is handed the base prompt again, or every agent below
    the first level would run without it (the workspace-instructions
    precedent, ``_build_sub_executor``)."""
    pool = _pool(monkeypatch, agents={"ag-1": _agent("ag-1"), "ag-2": _agent("ag-2")})
    ws = _Workspace("s-g", state_repo=await _state_repo(tmp_path))

    driver = await pool._build_executor(_graph_row("s-g", base_prompt=BASE), ws)
    parent = driver._executor
    sub = _two_node_graph()
    child = await parent._build_sub_executor(_GraphNodeRef(id="sub-1", graph_id=sub.id), sub)

    for node_id in ("ag-1", "ag-2"):
        node_agent = await child._agent_resolver(node_id)
        assert node_agent.system_prompt == [BASE, "sys", FRAGMENT], node_id


# ---------------------------------------------------------------------------
# R3 -- an invoked child graph does NOT inherit the caller's base prompt
# ---------------------------------------------------------------------------


async def test_a_child_executor_of_an_invoked_graph_does_not_inherit_the_base_prompt(
    monkeypatch, tmp_path,
):
    """A delegated run does not inherit the caller session's base prompt
    (the deliberate W4 boundary): the child build is handed the holder
    session, and even with the base prompt value sitting on it, no node
    agent of the child receives it."""
    pool = _pool(monkeypatch, agents={"ag-1": _agent("ag-1"), "ag-2": _agent("ag-2")})
    ws = _Workspace("s-1", state_repo=await _state_repo(tmp_path))
    ws._session.base_prompt = BASE  # the caller's row carries it
    services = pool._build_graph_invocation_services(
        workspace=ws, workspace_session=ws._session, graph_session_id="s-1", initiated_by=None,
    )
    assert services is not None

    child = await services.build_child_executor(graph=_two_node_graph(), gsid="s-1__child")

    for node_id in ("ag-1", "ag-2"):
        node_agent = await child._agent_resolver(node_id)
        assert BASE not in node_agent.system_prompt, node_id
        assert node_agent.system_prompt == ["sys", FRAGMENT], node_id


# ---------------------------------------------------------------------------
# U7 -- the resume seam: the ACTUAL resume coordinator builds from the row
# ---------------------------------------------------------------------------


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


async def test_the_resume_coordinator_builds_an_executor_that_keeps_the_base_prompt(monkeypatch):
    """U7, production-shaped: drive the REAL engine resume seam -
    ``pool._run_engine_session`` -> ``session_resume_coordinator.resume_engine_session``
    -> ``inject_resume_and_continue`` - on a resumable ask_user park, with the
    REAL builder UNPATCHED. The executor the coordinator builds from the row
    re-read at dispatch carries the base prompt, so the resumed turn carries
    it with no resume plumbing (the same shape as the house harness in
    ``tests/worker/test_engine_session_resume.py``)."""
    # The ask_user resume hook registers on import (the harness's side effect).
    import primer.toolset.misc  # noqa: F401
    from primer.model.chat import Message, ToolCallPart
    from tests.worker.test_engine_session_resume import (
        _build_engine,
        _claim_session,
        _make_resumable_session,
    )

    sp = _FakeStorageProvider()
    await sp.get_storage(Agent).create(_agent())
    sid, tcid = "s-bp-resume", "tc-bp-1"
    sess = _make_resumable_session(
        sid,
        tool_name="ask_user",
        tool_call_id=tcid,
        resume_event_payload={"response": "the token, as instructed"},
        llm_messages=[
            Message(
                role="assistant",
                parts=[ToolCallPart(id=tcid, name="_misc__ask_user", arguments={"prompt": "what is the token?"})],
            ).model_dump(mode="json"),
        ],
    ).model_copy(update={"base_prompt": BASE})
    await sp.get_storage(WorkspaceSession).create(sess)

    engine = _build_engine(sp.get_storage(WorkspaceSession))
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                 # type: ignore[arg-type]
        storage=sp,                     # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-engine-resume"

    async def _resolve_llm(_agent_arg, _override=None):
        return object(), ResolvedModel(
            profile_id="prov-1--m-1", provider_id="prov-1", model_name="m-1", context_length=8000,
            config=ModelProfileConfig(),
        )

    monkeypatch.setattr(pool, "_resolve_llm", _resolve_llm)
    ws = _Workspace(sid)
    monkeypatch.setattr(pool, "_load_workspace_for_persist", _async_return(ws))

    # The REAL builder runs; the wrapper only OBSERVES what the coordinator built.
    built: list = []
    real_build = pool._build_agent_executor

    async def _capture(session, workspace):
        driver = await real_build(session, workspace)
        built.append(driver._executor)
        return driver

    monkeypatch.setattr(pool, "_build_agent_executor", _capture)

    lease = await _claim_session(engine, sid)
    await pool._run_engine_session(lease)

    assert built, "the resume coordinator never built the turn's executor"
    # The resumed turn's system prompt still leads with the base prompt,
    # byte-identical, off the row the dispatch re-read.
    assert built[0]._agent.system_prompt == [BASE, "sys", FRAGMENT]
    # The resume tail actually ran through the built executor: the answered
    # round was injected into the session's history via it.
    assert ws._session.committed, "the resume never injected the answered round"
    injected = ws._session.committed[-1].decode()
    assert "tool" in injected and tcid in injected


async def test_a_resumed_build_rereads_the_base_prompt_from_the_row(monkeypatch):
    """A resume re-reads the row from storage and rebuilds the executor from
    it (the seam ``session_resume_coordinator`` uses); the base prompt comes
    off the row, so the resumed turn carries it with no resume plumbing."""
    sp = _FakeStorageProvider()
    agent = _agent()
    row = _agent_row("s-r", agent, base_prompt=BASE)
    await sp.get_storage(WorkspaceSession).create(row)

    reread = await sp.get_storage(WorkspaceSession).get("s-r")
    assert reread is not None
    assert reread.base_prompt == BASE, "the row must carry the field back through storage"

    pool = _pool(monkeypatch)
    ws = _Workspace("s-r")
    first = await pool._build_executor(row, ws)
    second = await pool._build_executor(reread, ws)  # the resume turn builds from the re-read row

    assert first._executor._agent.system_prompt == [BASE, "sys", FRAGMENT]
    assert second._executor._agent.system_prompt == [BASE, "sys", FRAGMENT]
