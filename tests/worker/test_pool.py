"""Tests for matrix.worker.pool.WorkerPool — start/stop + heartbeat skeleton."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from matrix.model.scheduler import WorkerConfig
from matrix.model.session import (
    AgentSessionBinding,
    Session,
    SessionStatus,
)
from matrix.scheduler.in_memory import InMemoryScheduler
from matrix.worker.pool import WorkerPool


@pytest.fixture
async def scheduler():
    s = InMemoryScheduler()
    await s.initialize()
    yield s
    await s.aclose()


@pytest.fixture
def worker_pool(scheduler):
    return WorkerPool(
        config=WorkerConfig(concurrency=2),
        scheduler=scheduler,
        storage=None,                  # type: ignore[arg-type]
        workspace_registry=None,       # type: ignore[arg-type]
        provider_registry=None,        # type: ignore[arg-type]
    )


async def test_pool_start_registers_worker(worker_pool, scheduler):
    await worker_pool.start()
    workers = await scheduler.list_workers()
    assert len(workers) == 1
    assert workers[0].capacity == 2
    await worker_pool.drain_and_stop()


async def test_pool_drain_deregisters_worker(worker_pool, scheduler):
    await worker_pool.start()
    await worker_pool.drain_and_stop()
    workers = await scheduler.list_workers()
    assert workers == []


async def test_pool_lease_ttl_propagates_to_scheduler(worker_pool, scheduler):
    await worker_pool.start()
    assert scheduler.lease_ttl_seconds == worker_pool.config.lease_ttl_seconds
    await worker_pool.drain_and_stop()


async def test_pool_worker_id_is_unique(scheduler):
    """Two pools registered against the same scheduler get distinct IDs."""
    p1 = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
    )
    p2 = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
    )
    await p1.start()
    await p2.start()
    try:
        assert p1.worker_id != p2.worker_id
        workers = await scheduler.list_workers()
        assert len(workers) == 2
    finally:
        await p1.drain_and_stop()
        await p2.drain_and_stop()


class _FakeExecutor:
    """Stand-in for WorkspaceAgentExecutor that records invoke()."""

    def __init__(self):
        self.invoked = False

    async def invoke(self, _messages):
        self.invoked = True


class _NoopPersist:
    async def persist_turn(self, turn_no): return None


async def _async_return(value):
    return value


async def test_run_one_turn_marks_complete(scheduler, monkeypatch):
    """Happy-path turn: claim, fake-invoke, complete_turn(SUCCESS)."""
    sid = "sess-rt-1"
    scheduler.register_session_for_test(
        sid, status=SessionStatus.RUNNING,
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    [lease] = await scheduler.claim("wrk-test", max_count=1)

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=lease.turn_no,
    )
    fake_executor = _FakeExecutor()
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_executor",
        lambda _s, _w: _async_return(fake_executor),
    )
    monkeypatch.setattr(
        pool, "_infer_post_turn_status",
        lambda _exec, _sess: SessionStatus.WAITING,
    )

    await pool._run_one_turn(lease)

    assert fake_executor.invoked is True
    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.turn_no == lease.turn_no + 1
    assert snapshot.status == SessionStatus.WAITING


async def test_run_one_turn_skips_when_session_already_ended(scheduler, monkeypatch):
    """If the session row says ENDED, don't run the turn — just release."""
    sid = "sess-ended-1"
    scheduler.register_session_for_test(
        sid, status=SessionStatus.ENDED,
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,            # type: ignore[arg-type]
        workspace_registry=None,                      # type: ignore[arg-type]
        provider_registry=None,                       # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    [lease] = await scheduler.claim("wrk-test", max_count=1)

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.ENDED,
        created_at=datetime.now(timezone.utc),
        turn_no=lease.turn_no,
    )
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    # _build_executor MUST NOT be called — if it is, raise.
    def _fail(*a, **kw):
        raise AssertionError("_build_executor should not be called for ENDED session")
    monkeypatch.setattr(pool, "_build_executor", _fail)

    await pool._run_one_turn(lease)

    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.ENDED


async def test_run_one_turn_honours_cancel_requested_flag(scheduler, monkeypatch):
    """If session.cancel_requested is True at claim time, end without
    running a turn."""
    sid = "sess-cancel-pre-1"
    scheduler.register_session_for_test(
        sid, status=SessionStatus.RUNNING,
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,            # type: ignore[arg-type]
        workspace_registry=None,                      # type: ignore[arg-type]
        provider_registry=None,                       # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    [lease] = await scheduler.claim("wrk-test", max_count=1)

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=lease.turn_no,
        cancel_requested=True,
    )
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    def _fail(*a, **kw):
        raise AssertionError("_build_executor should not be called")
    monkeypatch.setattr(pool, "_build_executor", _fail)

    await pool._run_one_turn(lease)

    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.ENDED


async def test_run_one_turn_honours_pause_requested_flag(scheduler, monkeypatch):
    """If session.pause_requested is True at claim time, transition to
    PAUSED without running a turn."""
    sid = "sess-pause-pre-1"
    scheduler.register_session_for_test(
        sid, status=SessionStatus.RUNNING,
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,            # type: ignore[arg-type]
        workspace_registry=None,                      # type: ignore[arg-type]
        provider_registry=None,                       # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    [lease] = await scheduler.claim("wrk-test", max_count=1)

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=lease.turn_no,
        pause_requested=True,
    )
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    def _fail(*a, **kw):
        raise AssertionError("_build_executor should not be called")
    monkeypatch.setattr(pool, "_build_executor", _fail)

    await pool._run_one_turn(lease)

    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.PAUSED


async def test_claim_loop_runs_runnable_session(scheduler, monkeypatch):
    """End-to-end: enqueue a session, start the pool, the claim loop
    picks it up, _run_one_turn runs (with a fake executor), and the
    session transitions to WAITING."""
    sid = "sess-claim-loop-1"
    scheduler.register_session_for_test(sid)

    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=1, poll_interval_seconds=0.1,
            heartbeat_interval_seconds=10,
        ),
        scheduler=scheduler,
        storage=None,                 # type: ignore[arg-type]
        workspace_registry=None,      # type: ignore[arg-type]
        provider_registry=None,       # type: ignore[arg-type]
    )

    class _OneTurnExecutor:
        async def invoke(self, _messages): return None

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))
    monkeypatch.setattr(pool, "_load_workspace_for_persist",
                        lambda _ws: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_executor",
                        lambda _s, _w: _async_return(_OneTurnExecutor()))
    monkeypatch.setattr(pool, "_infer_post_turn_status",
                        lambda _exec, _sess: SessionStatus.WAITING)

    await pool.start()
    try:
        await scheduler.enqueue(sid)
        # Wait until the session reaches WAITING (claim loop picked it up
        # and ran one turn).
        snapshot = scheduler.session_snapshot_for_test(sid)
        for _ in range(50):
            snapshot = scheduler.session_snapshot_for_test(sid)
            if snapshot.status == SessionStatus.WAITING:
                break
            await asyncio.sleep(0.05)
        assert snapshot.status == SessionStatus.WAITING
    finally:
        await pool.drain_and_stop()


async def test_run_one_turn_now_helper_executes_one_turn(scheduler, monkeypatch):
    """Public test helper: claim + run for a specific sid."""
    sid = "sess-helper-1"
    scheduler.register_session_for_test(sid)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))
    monkeypatch.setattr(pool, "_load_workspace_for_persist",
                        lambda _ws: _async_return(_NoopPersist()))

    class _Done:
        async def invoke(self, _messages): return None
    monkeypatch.setattr(pool, "_build_executor",
                        lambda _s, _w: _async_return(_Done()))
    monkeypatch.setattr(pool, "_infer_post_turn_status",
                        lambda _e, _s: SessionStatus.WAITING)

    await scheduler.enqueue(sid)
    await pool.run_one_turn_now(sid)

    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.WAITING


class _FakeAgentSessionForBuild:
    """Just enough AgentSession surface for WorkspaceAgentExecutor wiring.

    Exposes ``session_id`` + ``system_prompt_fragment`` (consumed by
    the executor's composite-prompt builder) and ``workspace_tools``
    (consumed by ``ToolExecutionManager.for_workspace``).
    """

    def __init__(self, sid: str) -> None:
        self.session_id = sid
        self.workspace_id = "ws-1"
        self.agent_id = "ag-1"
        self.workspace_tools: list = []
        self.system_prompt_fragment = "[fake workspace prompt]"


class _FakeWorkspaceForBuild:
    """Workspace stub that returns the fake AgentSession on get_session."""

    def __init__(self, sid: str) -> None:
        self.id = "ws-1"
        self._session = _FakeAgentSessionForBuild(sid)

    async def get_session(self, session_id):
        if session_id != self._session.session_id:
            return None
        return self._session


async def test_build_agent_executor_returns_turn_driver(monkeypatch):
    """Smoke: _build_executor for an agent binding constructs a
    WorkspaceAgentExecutor (wrapped in _TurnDriver) without raising
    NotImplementedError."""
    from matrix.agent.workspace_executor import WorkspaceAgentExecutor
    from matrix.model.agent import Agent, AgentModel
    from matrix.model.provider import LLMModel
    from matrix.worker.pool import _TurnDriver

    sid = "sess-build-1"
    agent = Agent(
        id="ag-1",
        description="test agent",
        model=AgentModel(provider_id="prov-1", model_name="m-1"),
        tools=[],
        system_prompt=["sys"],
    )
    session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(
            agent_id="ag-1", agent_snapshot=agent,
        ),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )
    workspace = _FakeWorkspaceForBuild(sid)

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                 # type: ignore[arg-type]
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
    )

    # Stub the provider registry calls — the registry instance itself
    # would otherwise need a storage_provider just to look up the LLM.
    fake_llm = object()
    fake_llm_model = LLMModel(name="m-1", context_length=8000)

    async def _get_llm(provider_id):
        assert provider_id == "prov-1"
        return fake_llm

    async def _get_toolset(_id):
        raise AssertionError("agent has no toolsets registered")

    async def _resolve_llm_model(_agent):
        return fake_llm_model

    monkeypatch.setattr(
        pool, "_provider_registry",
        type("R", (), {"get_llm": staticmethod(_get_llm),
                       "get_toolset": staticmethod(_get_toolset)})(),
    )
    monkeypatch.setattr(pool, "_resolve_llm_model", _resolve_llm_model)

    driver = await pool._build_executor(session, workspace)
    assert isinstance(driver, _TurnDriver)
    # The wrapped executor is the real WorkspaceAgentExecutor.
    assert isinstance(driver._executor, WorkspaceAgentExecutor)
    # The on-disk session was looked up off the workspace.
    assert driver._executor.session is workspace._session
    # last_done_reason starts unset.
    assert driver.last_done_reason is None


async def test_build_executor_raises_for_graph_binding_without_state_repo(monkeypatch):
    """When the workspace exposes no state_repo (sandbox / container /
    k8s backends today), graph dispatch must raise a clear ConfigError
    naming the workspace and the missing attribute. The default
    Workspace.state_repo is None, so backends opt in by override —
    legacy fakes get the helpful error."""
    from matrix.model.except_ import ConfigError
    from matrix.model.graph import Graph, _AgentNodeRef, _TerminalNode, _StaticEdge
    from matrix.model.session import GraphSessionBinding

    sid = "sess-graph-1"
    graph_snapshot = Graph(
        id="g-1", description="t",
        nodes=[
            _AgentNodeRef(id="start", agent_id="ag-1"),
            _TerminalNode(id="end"),
        ],
        edges=[_StaticEdge(from_node="start", to_node="end")],
        entry_node_id="start",
    )
    session = Session(
        id=sid, workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id="g-1", graph_snapshot=graph_snapshot),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                 # type: ignore[arg-type]
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
    )
    # _FakeWorkspaceForBuild does not override state_repo, so the ABC
    # default (None) flows through and triggers the ConfigError.
    with pytest.raises(ConfigError, match="state_repo"):
        await pool._build_executor(session, _FakeWorkspaceForBuild(sid))


async def test_build_graph_executor_returns_graph_turn_driver(monkeypatch, tmp_path):
    """Smoke: _build_executor for a graph binding constructs a
    WorkspaceGraphExecutor wrapped in _GraphTurnDriver, against a
    workspace with a real LocalStateRepo. The driver's last_done_reason
    is the 'graph_ended' sentinel that the post-turn mapper recognises."""
    from matrix.graph.workspace_executor import WorkspaceGraphExecutor
    from matrix.model.graph import (
        Graph,
        _AgentNodeRef,
        _StaticEdge,
        _TerminalNode,
    )
    from matrix.model.session import GraphSessionBinding
    from matrix.worker.pool import _GraphTurnDriver
    from matrix.workspace.local.state import LocalStateRepo

    sid = "sess-graph-ok-1"
    graph_snapshot = Graph(
        id="g-ok-1", description="2-node smoke graph",
        nodes=[
            _AgentNodeRef(id="start", agent_id="ag-1"),
            _TerminalNode(id="end"),
        ],
        edges=[_StaticEdge(from_node="start", to_node="end")],
        entry_node_id="start",
    )
    session = Session(
        id=sid, workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id="g-ok-1", graph_snapshot=graph_snapshot),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )

    # Workspace stub: only state_repo + id are read by _build_graph_executor.
    repo = LocalStateRepo(tmp_path / "state", workspace_id="ws-1")
    await repo.initialize()

    class _LocalWsStub:
        def __init__(self, sr):
            self.id = "ws-1"
            self.state_repo = sr

        async def get_session(self, session_id):
            # Phase 2 graph dispatch reads the holder AgentSession via
            # workspace.get_session(session.id). For this unit test we
            # exercise the no-holder fallback path (workspace_session
            # is None → ToolExecutionManager() with no workspace tools).
            return None

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                 # type: ignore[arg-type]
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
    )
    driver = await pool._build_executor(session, _LocalWsStub(repo))
    assert isinstance(driver, _GraphTurnDriver)
    assert isinstance(driver._executor, WorkspaceGraphExecutor)
    # The sentinel that _infer_post_turn_status maps to ENDED.
    assert driver.last_done_reason == "graph_ended"


async def test_infer_post_turn_status_maps_graph_ended_to_ended():
    """The mapper recognises the graph driver's sentinel and transitions
    the session directly to ENDED — no re-enqueue."""
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                 # type: ignore[arg-type]
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
    )

    class _Driver:
        last_done_reason = "graph_ended"

    # Session value is unused by the agent-flavor mapper today, but
    # we pass a stub to satisfy the signature.
    sess = Session(
        id="s", workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="a"),  # type: ignore[arg-type]
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )
    assert pool._infer_post_turn_status(_Driver(), sess) == SessionStatus.ENDED


async def test_infer_post_turn_status_reads_last_done_reason():
    """The mapper consults executor.last_done_reason and maps the
    documented stop-reason set to RUNNING / WAITING."""
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                 # type: ignore[arg-type]
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
    )
    session = Session(
        id="s", workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )

    class _Exec:
        last_done_reason: str | None = None

    e = _Exec()
    # Default: no Done observed -> RUNNING.
    e.last_done_reason = None
    assert pool._infer_post_turn_status(e, session) == SessionStatus.RUNNING
    # Clean stops -> RUNNING (more turns may follow).
    for r in ("stop", "end_turn", "stop_sequence", "tool_use"):
        e.last_done_reason = r
        assert pool._infer_post_turn_status(e, session) == SessionStatus.RUNNING
    # Operator-attention reasons -> WAITING.
    for r in ("max_tokens", "error", "content_filter"):
        e.last_done_reason = r
        assert pool._infer_post_turn_status(e, session) == SessionStatus.WAITING


async def test_metrics_snapshot_includes_in_flight_and_capacity(scheduler):
    """metrics_snapshot exposes the spec §14 worker keys."""
    pool = WorkerPool(
        config=WorkerConfig(concurrency=3),
        scheduler=scheduler,
        storage=None,                  # type: ignore[arg-type]
        workspace_registry=None,       # type: ignore[arg-type]
        provider_registry=None,        # type: ignore[arg-type]
    )
    snap = pool.metrics_snapshot()
    # Required keys per spec §14 (worker side).
    assert "matrix_worker_id" in snap
    assert "matrix_worker_in_flight" in snap
    assert "matrix_worker_capacity" in snap
    assert "matrix_worker_claims_total" in snap
    assert "matrix_session_turns_total" in snap
    assert "matrix_session_turn_duration_seconds" in snap
    # Capacity tracks the configured concurrency.
    assert snap["matrix_worker_capacity"] == 3
    # Nothing has run yet — in_flight is 0, counters 0, dicts empty.
    assert snap["matrix_worker_in_flight"] == 0
    assert snap["matrix_worker_claims_total"] == 0
    assert snap["matrix_session_turns_total"] == {}
    assert snap["matrix_session_turn_duration_seconds"]["count"] == 0


async def test_metrics_records_turn_outcome_after_run_one_turn(
    scheduler, monkeypatch,
):
    """After a happy-path turn, the success counter and duration aggregates
    are bumped."""
    sid = "sess-metrics-1"
    scheduler.register_session_for_test(sid, status=SessionStatus.RUNNING)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-metrics"
    await scheduler.register_worker(
        worker_id="wrk-metrics", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    [lease] = await scheduler.claim("wrk-metrics", max_count=1)
    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=lease.turn_no,
    )
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_executor",
        lambda _s, _w: _async_return(_FakeExecutor()),
    )
    monkeypatch.setattr(
        pool, "_infer_post_turn_status",
        lambda _e, _s: SessionStatus.WAITING,
    )

    await pool._run_one_turn(lease)

    snap = pool.metrics_snapshot()
    assert snap["matrix_session_turns_total"].get("success") == 1
    assert snap["matrix_session_turn_duration_seconds"]["count"] == 1
    assert snap["matrix_session_turn_duration_seconds"]["sum"] >= 0.0
    # In-flight cleared in the finally block.
    assert snap["matrix_worker_in_flight"] == 0


async def test_turn_driver_drains_async_generator():
    """_TurnDriver.invoke must consume an async-generator executor to
    completion, then expose the underlying ``last_done_reason``."""
    from matrix.worker.pool import _TurnDriver

    class _StreamingExecutor:
        def __init__(self) -> None:
            self.last_done_reason: str | None = None
            self.events_yielded = 0

        async def invoke(self, _messages, *, response_format=None):
            for i in range(3):
                self.events_yielded += 1
                yield i
            self.last_done_reason = "end_turn"

    raw = _StreamingExecutor()
    driver = _TurnDriver(raw)
    await driver.invoke([])
    assert raw.events_yielded == 3
    assert driver.last_done_reason == "end_turn"


async def test_cancel_loop_routes_to_active_scope(scheduler, monkeypatch):
    """signal_cancel arrives via _cancel_loop and fires the matching
    in-flight scope.cancel — proves the wiring (spec §7 step 5)."""
    sid = "sess-cancel-loop-1"
    scheduler.register_session_for_test(sid)

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, poll_interval_seconds=0.1),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
    )

    class _SleepingExecutor:
        async def invoke(self, _m): await asyncio.sleep(60)

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        cancel_requested=False,
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))
    monkeypatch.setattr(pool, "_load_workspace_for_persist",
                        lambda _ws: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_executor",
                        lambda _s, _w: _async_return(_SleepingExecutor()))

    await pool.start()
    try:
        await scheduler.enqueue(sid)
        # Wait for the scope to be registered.
        for _ in range(50):
            if sid in pool._active_scopes:
                break
            await asyncio.sleep(0.05)
        assert sid in pool._active_scopes

        fake_session.cancel_requested = True
        await scheduler.signal_cancel(sid)

        # Wait for the session to transition to ENDED.
        snapshot = scheduler.session_snapshot_for_test(sid)
        for _ in range(100):
            snapshot = scheduler.session_snapshot_for_test(sid)
            if snapshot.status == SessionStatus.ENDED:
                break
            await asyncio.sleep(0.05)
        assert snapshot.status == SessionStatus.ENDED
    finally:
        await pool.drain_and_stop()
