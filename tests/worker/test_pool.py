"""Tests for matrix.worker.pool.WorkerPool — start/stop + heartbeat skeleton."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    WorkspaceSession,
    SessionStatus,
)
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease as ClaimLease, ReleaseOutcome
from primer.int.scheduler import Lease as SchedLease
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool


@pytest.fixture
async def scheduler():
    s = InMemoryScheduler()
    await s.initialize()
    yield s
    await s.aclose()


@pytest.fixture
def engine():
    return InMemoryClaimEngine(adapters={})


def _make_sched_lease(
    session_id: str,
    worker_id: str,
    turn_no: int = 0,
) -> SchedLease:
    """Build a scheduler Lease without calling scheduler.claim()."""
    return SchedLease(
        session_id=session_id,
        worker_id=worker_id,
        expires_at=datetime.now(timezone.utc),
        attempt_count=0,
        turn_no=turn_no,
    )


@pytest.fixture
def worker_pool(scheduler, engine):
    return WorkerPool(
        config=WorkerConfig(concurrency=2),
        scheduler=scheduler,
        storage=None,                  # type: ignore[arg-type]
        workspace_registry=None,       # type: ignore[arg-type]
        provider_registry=None,        # type: ignore[arg-type]
        engine=engine,
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


async def test_pool_worker_id_is_unique(scheduler, engine):
    """Two pools registered against the same scheduler get distinct IDs."""
    p1 = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
        engine=engine,
    )
    p2 = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
        engine=engine,
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


async def test_run_one_turn_marks_complete(scheduler, engine, monkeypatch):
    """Happy-path turn: fake-invoke, complete_turn(SUCCESS)."""
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
        engine=engine,
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    from primer.scheduler.in_memory import _LeaseState
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-test", runnable=True)
    lease = _make_sched_lease(sid, "wrk-test")

    fake_session = WorkspaceSession(
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


async def test_run_one_turn_skips_when_session_already_ended(scheduler, engine, monkeypatch):
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
        engine=engine,
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    from primer.scheduler.in_memory import _LeaseState
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-test", runnable=True)
    lease = _make_sched_lease(sid, "wrk-test")

    fake_session = WorkspaceSession(
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


async def test_run_one_turn_honours_cancel_requested_flag(scheduler, engine, monkeypatch):
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
        engine=engine,
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    from primer.scheduler.in_memory import _LeaseState
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-test", runnable=True)
    lease = _make_sched_lease(sid, "wrk-test")

    fake_session = WorkspaceSession(
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


async def test_run_one_turn_honours_pause_requested_flag(scheduler, engine, monkeypatch):
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
        engine=engine,
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    from primer.scheduler.in_memory import _LeaseState
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-test", runnable=True)
    lease = _make_sched_lease(sid, "wrk-test")

    fake_session = WorkspaceSession(
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


async def test_claim_loop_runs_runnable_session(scheduler, engine, monkeypatch):
    """End-to-end: enqueue a session via engine, start the pool, the claim
    loop picks it up, and dispatches to run_one_session_turn."""
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
        engine=engine,
    )

    dispatched: list = []

    async def _fake_run_one_session_turn(lease, deps):
        dispatched.append(lease.entity_id)
        return ReleaseOutcome(success=True, drop_lease=True)

    await pool.start()
    try:
        with patch(
            "primer.worker.pool.run_one_session_turn",
            side_effect=_fake_run_one_session_turn,
        ):
            await engine.upsert(ClaimKind.SESSION, sid, priority=100)
            # Wait until run_one_session_turn is called.
            for _ in range(50):
                if sid in dispatched:
                    break
                await asyncio.sleep(0.05)
        assert sid in dispatched, (
            "claim loop should have dispatched to run_one_session_turn"
        )
    finally:
        await pool.drain_and_stop()


async def test_run_one_turn_now_helper_executes_one_turn(scheduler, engine, monkeypatch):
    """Public test helper: engine.upsert + run_one_turn_now dispatches to
    run_one_session_turn."""
    from primer.int.claim import ClaimKind
    sid = "sess-helper-1"
    scheduler.register_session_for_test(sid)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )

    dispatched: list = []

    async def _fake_run_one_session_turn(lease, deps):
        dispatched.append(lease.entity_id)
        return ReleaseOutcome(success=True, drop_lease=True)

    # Seed the engine lease so run_one_turn_now can claim it.
    await engine.upsert(ClaimKind.SESSION, sid, priority=100)

    with patch(
        "primer.worker.pool.run_one_session_turn",
        side_effect=_fake_run_one_session_turn,
    ):
        await pool.run_one_turn_now(sid)

    assert sid in dispatched, (
        "run_one_turn_now should dispatch to run_one_session_turn"
    )


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
    from primer.agent.workspace_executor import WorkspaceAgentExecutor
    from primer.model.agent import Agent, AgentModel
    from primer.model.provider import LLMModel
    from primer.worker.pool import _TurnDriver

    sid = "sess-build-1"
    agent = Agent(
        id="ag-1",
        description="test agent",
        model=AgentModel(provider_id="prov-1", model_name="m-1"),
        tools=[],
        system_prompt=["sys"],
    )
    session = WorkspaceSession(
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
        engine=InMemoryClaimEngine(adapters={}),
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
    from primer.model.except_ import ConfigError
    from primer.model.graph import Graph, _AgentNodeRef, _TerminalNode, _StaticEdge
    from primer.model.workspace_session import GraphSessionBinding

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
    session = WorkspaceSession(
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
        engine=InMemoryClaimEngine(adapters={}),
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
    from primer.graph.workspace_executor import WorkspaceGraphExecutor
    from primer.model.graph import (
        Graph,
        _AgentNodeRef,
        _StaticEdge,
        _TerminalNode,
    )
    from primer.model.workspace_session import GraphSessionBinding
    from primer.worker.pool import _GraphTurnDriver
    from primer.workspace.local.state import LocalStateRepo

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
    session = WorkspaceSession(
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
        engine=InMemoryClaimEngine(adapters={}),
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
        engine=InMemoryClaimEngine(adapters={}),
    )

    class _Driver:
        last_done_reason = "graph_ended"

    # WorkspaceSession value is unused by the agent-flavor mapper today, but
    # we pass a stub to satisfy the signature.
    sess = WorkspaceSession(
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
        engine=InMemoryClaimEngine(adapters={}),
    )
    session = WorkspaceSession(
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


async def test_metrics_snapshot_includes_in_flight_and_capacity(scheduler, engine):
    """metrics_snapshot exposes the spec §14 worker keys."""
    pool = WorkerPool(
        config=WorkerConfig(concurrency=3),
        scheduler=scheduler,
        storage=None,                  # type: ignore[arg-type]
        workspace_registry=None,       # type: ignore[arg-type]
        provider_registry=None,        # type: ignore[arg-type]
        engine=engine,
    )
    snap = pool.metrics_snapshot()
    # Required keys per spec §14 (worker side).
    assert "primer_worker_id" in snap
    assert "primer_worker_in_flight" in snap
    assert "primer_worker_capacity" in snap
    assert "primer_worker_claims_total" in snap
    assert "matrix_session_turns_total" in snap
    assert "matrix_session_turn_duration_seconds" in snap
    # Capacity tracks the configured concurrency.
    assert snap["primer_worker_capacity"] == 3
    # Nothing has run yet — in_flight is 0, counters 0, dicts empty.
    assert snap["primer_worker_in_flight"] == 0
    assert snap["primer_worker_claims_total"] == 0
    assert snap["matrix_session_turns_total"] == {}
    assert snap["matrix_session_turn_duration_seconds"]["count"] == 0


async def test_metrics_records_turn_outcome_after_run_one_turn(
    scheduler, engine, monkeypatch,
):
    """After a happy-path turn, the success counter and duration aggregates
    are bumped."""
    from primer.scheduler.in_memory import _LeaseState
    sid = "sess-metrics-1"
    scheduler.register_session_for_test(sid, status=SessionStatus.RUNNING)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-metrics"
    await scheduler.register_worker(
        worker_id="wrk-metrics", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-metrics", runnable=True)
    lease = _make_sched_lease(sid, "wrk-metrics")
    fake_session = WorkspaceSession(
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
    assert snap["primer_worker_in_flight"] == 0


async def test_turn_driver_drains_async_generator():
    """_TurnDriver.invoke must consume an async-generator executor to
    completion, then expose the underlying ``last_done_reason``."""
    from primer.worker.pool import _TurnDriver

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


async def test_cancel_loop_routes_to_active_scope(scheduler, engine, monkeypatch):
    """Session claims dispatch to run_one_session_turn; the claim loop
    picks up the session and calls the new handler (cancel wiring is
    tested in tests/session/test_dispatch.py — here we just verify the
    turn is dispatched and completes without the pool raising)."""
    sid = "sess-cancel-loop-1"
    scheduler.register_session_for_test(sid)

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, poll_interval_seconds=0.1),
        scheduler=scheduler, storage=None,             # type: ignore[arg-type]
        workspace_registry=None,                       # type: ignore[arg-type]
        provider_registry=None,                        # type: ignore[arg-type]
        engine=engine,
    )

    dispatched: list = []

    async def _fake_run_one_session_turn(lease, deps):
        dispatched.append(lease.entity_id)
        return ReleaseOutcome(success=True, drop_lease=True)

    await pool.start()
    try:
        with patch(
            "primer.worker.pool.run_one_session_turn",
            side_effect=_fake_run_one_session_turn,
        ):
            await engine.upsert(ClaimKind.SESSION, sid, priority=100)
            for _ in range(50):
                if sid in dispatched:
                    break
                await asyncio.sleep(0.05)

        assert sid in dispatched, "claim loop should dispatch session to run_one_session_turn"
        # Session path no longer uses _active_scopes (cancel is handled by
        # _cancel_watcher inside run_one_session_turn via event bus).
        assert sid not in pool._active_scopes
    finally:
        await pool.drain_and_stop()


# ---------------------------------------------------------------------------
# Task 8: _run_engine_session dispatches to run_one_session_turn
# ---------------------------------------------------------------------------


def _make_claim_lease(
    entity_id: str,
    *,
    kind: ClaimKind = ClaimKind.SESSION,
    worker_id: str = "wrk-test",
) -> ClaimLease:
    """Build a ClaimEngine Lease for testing."""
    return ClaimLease(
        kind=kind,
        entity_id=entity_id,
        claimed_by=worker_id,
        claimed_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
        attempt_count=0,
        last_error=None,
    )


async def test_run_engine_session_dispatches_to_run_one_session_turn(
    scheduler, engine, monkeypatch,
):
    """_run_engine_session must call run_one_session_turn (not _run_one_turn)."""
    sid = "sess-new-dispatch-1"
    scheduler.register_session_for_test(sid, status=SessionStatus.RUNNING)

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=None,               # type: ignore[arg-type]
        workspace_registry=None,    # type: ignore[arg-type]
        provider_registry=None,     # type: ignore[arg-type]
        engine=engine,
        event_bus=None,
    )
    pool._worker_id = "wrk-test"

    fake_session = WorkspaceSession(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))

    called_with: list = []

    async def _fake_run_one_session_turn(lease, deps):
        called_with.append((lease, deps))
        return ReleaseOutcome(success=True, drop_lease=True)

    # Patch at the module level where pool.py imports it.
    with patch(
        "primer.worker.pool.run_one_session_turn",
        side_effect=_fake_run_one_session_turn,
    ):
        engine_lease = _make_claim_lease(sid)
        await pool._run_engine_session(engine_lease)

    assert len(called_with) == 1, "run_one_session_turn should be called exactly once"
    lease_arg, deps_arg = called_with[0]
    assert lease_arg.entity_id == sid
    assert lease_arg.kind == ClaimKind.SESSION
    # Deps bundle must supply the required attributes.
    from primer.session.dispatch import SessionDispatchDeps
    assert isinstance(deps_arg, SessionDispatchDeps)
    assert deps_arg.storage_provider is pool._storage
    assert deps_arg.event_bus is pool._event_bus
    assert callable(deps_arg.build_executor)


async def test_run_engine_session_releases_engine_lease_on_success(
    scheduler, engine, monkeypatch,
):
    """_run_engine_session must call engine.release with the engine lease
    (not drop it silently) after run_one_session_turn returns."""
    sid = "sess-release-1"
    scheduler.register_session_for_test(sid, status=SessionStatus.RUNNING)

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=None,               # type: ignore[arg-type]
        workspace_registry=None,    # type: ignore[arg-type]
        provider_registry=None,     # type: ignore[arg-type]
        engine=engine,
        event_bus=None,
    )
    pool._worker_id = "wrk-test"

    fake_session = WorkspaceSession(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))

    released: list = []
    orig_release = engine.release

    async def _capture_release(lease, *, outcome):
        released.append((lease, outcome))
        return await orig_release(lease, outcome=outcome)

    monkeypatch.setattr(engine, "release", _capture_release)

    engine_lease = _make_claim_lease(sid)
    # Seed the lease in the engine so release() can find it.
    await engine.upsert(ClaimKind.SESSION, sid, priority=100)

    with patch(
        "primer.worker.pool.run_one_session_turn",
        return_value=ReleaseOutcome(success=True, drop_lease=True),
    ):
        await pool._run_engine_session(engine_lease)

    # engine.release was called with the original engine lease.
    assert len(released) == 1
    assert released[0][0].entity_id == sid
    assert released[0][1].success is True


async def test_build_session_executor_returns_callable(scheduler, engine, monkeypatch):
    """pool._build_session_executor(session) returns an awaitable executor
    by delegating to _build_executor after resolving the workspace."""
    sid = "sess-bse-1"
    fake_session = WorkspaceSession(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=None,               # type: ignore[arg-type]
        workspace_registry=None,    # type: ignore[arg-type]
        provider_registry=None,     # type: ignore[arg-type]
        engine=engine,
        event_bus=None,
    )
    pool._worker_id = "wrk-test"

    class _FakeWs:
        id = "ws-1"

    fake_ws = _FakeWs()
    monkeypatch.setattr(pool, "_load_workspace_for_persist",
                        lambda _ws_id: _async_return(fake_ws))

    class _FakeExecutor:
        async def invoke(self, _messages):
            if False:
                yield  # make it an async generator

    fake_driver = _FakeExecutor()
    monkeypatch.setattr(pool, "_build_executor",
                        lambda _s, _w: _async_return(fake_driver))

    result = await pool._build_session_executor(fake_session)
    assert result is fake_driver


# ===========================================================================
# _WorkspaceIOShim
# ===========================================================================


@pytest.mark.asyncio
async def test_workspace_io_shim_delegates_to_workspace():
    """_WorkspaceIOShim.append_message_line forwards to workspace.append_message_line."""
    from primer.worker.pool import _WorkspaceIOShim

    received: list[tuple[str, bytes]] = []

    class _FakeWorkspace:
        async def append_message_line(self, session_id: str, line: bytes) -> None:
            received.append((session_id, line))

    class _FakeRegistry:
        async def get_workspace(self, workspace_id: str):
            return _FakeWorkspace()

    shim = _WorkspaceIOShim(workspace_registry=_FakeRegistry())
    shim.register_session("sess-1", "ws-1")

    await shim.append_message_line("sess-1", b'{"seq":1}\n')
    assert len(received) == 1
    assert received[0] == ("sess-1", b'{"seq":1}\n')


@pytest.mark.asyncio
async def test_workspace_io_shim_warns_when_no_registry():
    """_WorkspaceIOShim drops the line and logs a warning when no registry."""
    import logging
    from primer.worker.pool import _WorkspaceIOShim

    shim = _WorkspaceIOShim(workspace_registry=None)
    shim.register_session("sess-1", "ws-1")

    # Should not raise; the line is silently dropped.
    await shim.append_message_line("sess-1", b'{"seq":1}\n')


@pytest.mark.asyncio
async def test_workspace_io_shim_warns_when_no_mapping():
    """_WorkspaceIOShim drops the line when session has no workspace mapping."""
    from primer.worker.pool import _WorkspaceIOShim

    class _FakeRegistry:
        async def get_workspace(self, workspace_id: str):
            return None

    shim = _WorkspaceIOShim(workspace_registry=_FakeRegistry())
    # No register_session() call — the shim has no workspace_id mapping.
    await shim.append_message_line("sess-orphan", b'{"seq":1}\n')
    # Should complete without error.
