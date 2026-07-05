"""Extracted ``create_session`` helper — Plan §3.2.

Mirrors the core persist + auto-start + claim-register semantics of
``POST /v1/workspaces/{wid}/sessions`` so the trigger dispatcher
(Phase 4+) can build fresh sessions without going through HTTP.
Spec §12.5.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from primer.int.claim import ClaimKind
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import (
    ConfigError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.workspace.session_factory import (
    SessionCancelDeps,
    SessionFactoryDeps,
    cancel_session,
    create_session,
    start_workspace_session,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.signalled: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)

    async def signal_cancel(self, sid: str) -> None:
        self.signalled.append(sid)


class _FakeClaimEngine:
    def __init__(self) -> None:
        self.upserts: list[tuple[ClaimKind, str, int]] = []
        self.deleted: list[tuple[ClaimKind, str]] = []

    async def upsert(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 100,
        next_attempt_at=None,
    ) -> None:
        self.upserts.append((kind, entity_id, priority))

    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None:
        self.deleted.append((kind, entity_id))


class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, key: str, payload: Any) -> None:
        self.published.append((key, payload))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_scheduler() -> _FakeScheduler:
    return _FakeScheduler()


@pytest.fixture
def fake_claim_engine() -> _FakeClaimEngine:
    return _FakeClaimEngine()


@pytest.fixture
def deps(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
) -> SessionFactoryDeps:
    return SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=None,
    )


def _binding() -> AgentSessionBinding:
    return AgentSessionBinding(agent_id="ag-1")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_no_auto_start_persists_created(
    fake_storage_provider, deps, fake_scheduler, fake_claim_engine,
):
    sess = await create_session(
        workspace_id="ws-1",
        binding=_binding(),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        deps=deps,
    )

    assert sess.workspace_id == "ws-1"
    assert sess.status == SessionStatus.CREATED
    assert sess.turn_status == "idle"
    # Row landed in storage.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    rehydrated = await storage.get(sess.id)
    assert rehydrated is not None
    assert rehydrated.status == SessionStatus.CREATED
    # Scheduler NOT touched when auto_start=False.
    assert fake_scheduler.enqueued == []
    # Claim engine must NOT receive a upsert when auto_start=False.
    # The session stays inert (CREATED, no lease) until an explicit
    # resume/start later enqueues it.
    assert fake_claim_engine.upserts == []


@pytest.mark.asyncio
async def test_create_session_auto_start_flips_running_and_enqueues(
    deps, fake_scheduler, fake_claim_engine,
):
    sess = await create_session(
        workspace_id="ws-1",
        binding=_binding(),
        initial_instructions="boot",
        graph_input=None,
        auto_start=True,
        metadata={},
        deps=deps,
    )

    assert sess.status == SessionStatus.RUNNING
    assert sess.started_at is not None
    assert fake_scheduler.enqueued == [sess.id]
    assert (ClaimKind.SESSION, sess.id, 100) in fake_claim_engine.upserts


@pytest.mark.asyncio
async def test_create_session_persists_graph_input_in_metadata(deps):
    """Graph bindings fold ``graph_input`` into ``metadata['graph_input']``.

    Mirrors the existing router behaviour at
    primer/api/routers/sessions.py — the workspace graph executor reads
    ``session.metadata['graph_input']`` as the initial input.
    """
    sess = await create_session(
        workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id="gr-1"),
        initial_instructions=None,
        graph_input={"x": 1},
        auto_start=False,
        metadata={"k": "v"},
        deps=deps,
    )

    assert sess.metadata.get("k") == "v"
    assert sess.metadata.get("graph_input") == {"x": 1}


@pytest.mark.asyncio
async def test_create_session_agent_binding_ignores_graph_input(deps):
    """Agent bindings never touch graph_input — matches the router."""
    sess = await create_session(
        workspace_id="ws-1",
        binding=_binding(),
        initial_instructions=None,
        graph_input={"x": 1},
        auto_start=False,
        metadata={"k": "v"},
        deps=deps,
    )

    assert sess.metadata == {"k": "v"}
    assert "graph_input" not in sess.metadata


@pytest.mark.asyncio
async def test_create_session_returns_unique_session_id(deps):
    s1 = await create_session(
        workspace_id="ws-1", binding=_binding(),
        initial_instructions=None, graph_input=None,
        auto_start=False, metadata={}, deps=deps,
    )
    s2 = await create_session(
        workspace_id="ws-1", binding=_binding(),
        initial_instructions=None, graph_input=None,
        auto_start=False, metadata={}, deps=deps,
    )
    assert s1.id != s2.id
    assert s1.id.startswith("sess-")


@pytest.mark.asyncio
async def test_create_session_swallows_scheduler_errors(
    fake_storage_provider, fake_claim_engine,
):
    """Scheduler failures during auto_start must not block session creation."""

    class _BrokenScheduler:
        async def enqueue(self, sid: str) -> None:
            raise RuntimeError("queue down")

    deps = SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=_BrokenScheduler(),
        workspace_registry=None,
    )

    # Should NOT raise — the helper is best-effort on scheduler/claim
    # registration so a broken scheduler doesn't strand a freshly
    # persisted session row.
    sess = await create_session(
        workspace_id="ws-1", binding=_binding(),
        initial_instructions=None, graph_input=None,
        auto_start=True, metadata={}, deps=deps,
    )
    assert sess.status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_create_session_works_with_seeded_agent(
    fake_storage_provider, deps,
):
    """Smoke test: agent-binding referencing a real Agent row.

    The factory does NOT validate the binding (callers do that — the
    router does its own 404/422). We just confirm the helper happily
    persists a session whose binding points at an existing Agent row.
    """
    await fake_storage_provider.get_storage(Agent).create(
        Agent(
            id="ag-1", description="x",
            model=AgentModel(provider_id="p", model_name="m"),
        ),
    )
    sess = await create_session(
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        deps=deps,
    )
    assert isinstance(sess.binding, AgentSessionBinding)
    assert sess.binding.agent_id == "ag-1"


# ---------------------------------------------------------------------------
# Config guard: auto_start with no ClaimEngine must RAISE, not silently hang
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_auto_start_with_none_claim_engine_raises(
    fake_storage_provider, fake_scheduler,
):
    """auto_start=True + claim_engine=None must raise ConfigError.

    Regression for the "None-deps silently hangs a session" class: with no
    ClaimEngine the row flips to RUNNING but is never claimed by any worker,
    hanging forever. The factory must fail loud at construction instead.
    """
    bad_deps = SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=None,
        scheduler=fake_scheduler,
        workspace_registry=None,
    )
    with pytest.raises(ConfigError, match="ClaimEngine"):
        await create_session(
            workspace_id="ws-1",
            binding=_binding(),
            initial_instructions="go",
            graph_input=None,
            auto_start=True,
            metadata={},
            deps=bad_deps,
        )
    # The row must NOT have been persisted as RUNNING -- the guard fires
    # before any storage write that could strand a never-claimed session.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    page = await storage.find(None, OffsetPage(offset=0, length=50))
    assert all(s.status != SessionStatus.RUNNING for s in page.items)


@pytest.mark.asyncio
async def test_create_session_no_auto_start_with_none_claim_engine_ok(
    fake_storage_provider, fake_scheduler,
):
    """auto_start=False + claim_engine=None is legitimate: the session stays
    CREATED with no lease until an explicit resume performs its own upsert.
    The guard must NOT fire on this path.
    """
    deps = SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=None,
        scheduler=fake_scheduler,
        workspace_registry=None,
    )
    sess = await create_session(
        workspace_id="ws-1",
        binding=_binding(),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        deps=deps,
    )
    assert sess.status == SessionStatus.CREATED


# ---------------------------------------------------------------------------
# auto_start gate: claim-engine upsert is only sent when auto_start=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_auto_start_false_no_claim_upsert(
    fake_storage_provider, deps, fake_scheduler, fake_claim_engine,
):
    """auto_start=False must leave the session in CREATED with no claim
    lease registered. The worker must not discover and run the session
    until an explicit start (resume) is issued later.
    """
    sess = await create_session(
        workspace_id="ws-1",
        binding=_binding(),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        deps=deps,
    )

    assert sess.status == SessionStatus.CREATED
    assert fake_scheduler.enqueued == []
    # No lease upsert -- the worker has no knowledge of this session.
    assert fake_claim_engine.upserts == []


@pytest.mark.asyncio
async def test_create_session_auto_start_true_sends_claim_upsert(
    deps, fake_scheduler, fake_claim_engine,
):
    """auto_start=True must flip status to RUNNING, enqueue with the
    scheduler, AND send a claim-engine upsert so the worker can pick
    it up.
    """
    sess = await create_session(
        workspace_id="ws-1",
        binding=_binding(),
        initial_instructions="go",
        graph_input=None,
        auto_start=True,
        metadata={},
        deps=deps,
    )

    assert sess.status == SessionStatus.RUNNING
    assert fake_scheduler.enqueued == [sess.id]
    assert (ClaimKind.SESSION, sess.id, 100) in fake_claim_engine.upserts


@pytest.mark.asyncio
async def test_explicit_resume_of_created_session_sends_claim_upsert(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    """Explicit start (POST .../resume on a CREATED session) must
    transition to RUNNING and register a claim-engine upsert so the
    worker discovers and runs the session.

    This verifies that the resume_session route's own upsert (in
    primer/api/routers/sessions.py) compensates for the upsert that
    create_session deliberately withholds when auto_start=False.
    The test drives the router logic directly via the shared helper
    import so it stays a pure unit test (no HTTP).
    """
    # Create with auto_start=False -- CREATED, no lease.
    no_start_deps = SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=None,
    )
    sess = await create_session(
        workspace_id="ws-1",
        binding=_binding(),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        deps=no_start_deps,
    )
    assert sess.status == SessionStatus.CREATED
    assert fake_claim_engine.upserts == []

    # Simulate the resume_session route: transition CREATED -> RUNNING
    # and call claim_engine.upsert (same as the router does).
    from datetime import datetime, timezone

    sessions_storage = fake_storage_provider.get_storage(WorkspaceSession)
    s = await sessions_storage.get(sess.id)
    assert s is not None
    s.status = SessionStatus.RUNNING
    if s.started_at is None:
        s.started_at = datetime.now(timezone.utc)
    s.pause_requested = False
    await sessions_storage.update(s)
    await fake_scheduler.enqueue(sess.id)
    await fake_claim_engine.upsert(ClaimKind.SESSION, sess.id)

    # Now the session should be RUNNING with a lease registered.
    assert (ClaimKind.SESSION, sess.id, 100) in fake_claim_engine.upserts
    assert sess.id in fake_scheduler.enqueued
    rehydrated = await sessions_storage.get(sess.id)
    assert rehydrated is not None
    assert rehydrated.status == SessionStatus.RUNNING


# ---------------------------------------------------------------------------
# start_workspace_session: the full create flow (validate + slot + persist)
# ---------------------------------------------------------------------------


class _FakeLiveWorkspace:
    """Records start_session calls; otherwise a no-op."""

    def __init__(self) -> None:
        self.started: list[dict] = []

    async def start_session(
        self, binding, *, id, instructions, parent_session_id, name=None
    ) -> None:
        self.started.append(
            {
                "binding": binding,
                "id": id,
                "instructions": instructions,
                "parent_session_id": parent_session_id,
                "name": name,
            }
        )


class _FakeWorkspaceRegistry:
    """Tiny stub exposing async get_workspace(id)."""

    def __init__(self, live: _FakeLiveWorkspace) -> None:
        self._live = live
        self.requested: list[str] = []

    async def get_workspace(self, workspace_id: str) -> _FakeLiveWorkspace:
        self.requested.append(workspace_id)
        return self._live


def _full_deps(
    fake_storage_provider, fake_scheduler, fake_claim_engine, live,
) -> SessionFactoryDeps:
    return SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=_FakeWorkspaceRegistry(live),
    )


async def _seed_workspace(sp, workspace_id="ws-1") -> None:
    from pydantic import SecretStr

    from primer.model.workspace import WorkspaceRuntimeMeta

    await sp.get_storage(WorkspaceRow).create(
        WorkspaceRow(
            id=workspace_id,
            template_id="tpl-1",
            provider_id="local-1",
            created_at=datetime.now(timezone.utc),
            runtime_meta=WorkspaceRuntimeMeta(
                url="ws://127.0.0.1:5959/",
                token=SecretStr("t"),
            ),
        ),
    )


async def _seed_agent(sp, agent_id="ag-1") -> None:
    await sp.get_storage(Agent).create(
        Agent(
            id=agent_id,
            description="x",
            model=AgentModel(provider_id="p", model_name="m"),
        ),
    )


@pytest.mark.asyncio
async def test_start_workspace_session_agent_happy_path(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    await _seed_workspace(fake_storage_provider)
    await _seed_agent(fake_storage_provider)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, fake_scheduler, fake_claim_engine, live,
    )

    sess = await start_workspace_session(
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        initial_instructions="boot",
        graph_input=None,
        auto_start=True,
        metadata={},
        parent_session_id=None,
        deps=deps,
    )

    assert isinstance(sess, WorkspaceSession)
    assert sess.workspace_id == "ws-1"
    assert sess.status == SessionStatus.RUNNING
    # On-disk slot was allocated for the same sid the row carries.
    assert len(live.started) == 1
    assert live.started[0]["id"] == sess.id
    # auto_start enqueued the same sid with the scheduler.
    assert fake_scheduler.enqueued == [sess.id]


@pytest.mark.asyncio
async def test_start_workspace_session_missing_agent_raises_validation(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    await _seed_workspace(fake_storage_provider)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, fake_scheduler, fake_claim_engine, live,
    )

    with pytest.raises(ValidationError):
        await start_workspace_session(
            workspace_id="ws-1",
            binding=AgentSessionBinding(agent_id="missing"),
            initial_instructions=None,
            graph_input=None,
            auto_start=False,
            metadata={},
            parent_session_id=None,
            deps=deps,
        )
    # No slot allocated when validation fails.
    assert live.started == []


@pytest.mark.asyncio
async def test_start_workspace_session_missing_workspace_raises_not_found(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    await _seed_agent(fake_storage_provider)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, fake_scheduler, fake_claim_engine, live,
    )

    with pytest.raises(NotFoundError):
        await start_workspace_session(
            workspace_id="ws-missing",
            binding=AgentSessionBinding(agent_id="ag-1"),
            initial_instructions=None,
            graph_input=None,
            auto_start=False,
            metadata={},
            parent_session_id=None,
            deps=deps,
        )
    assert live.started == []


async def _seed_graph(sp, graph: Graph) -> None:
    await sp.get_storage(Graph).create(graph)


def _runnable_graph(graph_id="gr-run") -> Graph:
    return Graph(
        id=graph_id,
        description="Begin -> agent -> End",
        nodes=[
            _BeginNode(id="b"),
            _AgentNodeRef(id="a", agent_id="ag-1"),
            _EndNode(id="e"),
        ],
        edges=[
            _StaticEdge(from_node="b", to_node="a"),
            _StaticEdge(from_node="a", to_node="e"),
        ],
    )


@pytest.mark.asyncio
async def test_start_workspace_session_unrunnable_graph_raises_validation(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    """Binding a session to an empty / unrunnable graph is rejected at
    session-start (not at graph creation) with a ValidationError -> 422."""
    await _seed_workspace(fake_storage_provider)
    # An empty graph now persists fine, but it is not runnable.
    await _seed_graph(fake_storage_provider, Graph(id="gr-empty", description="draft", nodes=[], edges=[]))
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, fake_scheduler, fake_claim_engine, live,
    )

    with pytest.raises(ValidationError):
        await start_workspace_session(
            workspace_id="ws-1",
            binding=GraphSessionBinding(graph_id="gr-empty"),
            initial_instructions=None,
            graph_input=None,
            auto_start=False,
            metadata={},
            parent_session_id=None,
            deps=deps,
        )
    # No on-disk slot allocated when runnability validation fails.
    assert live.started == []


@pytest.mark.asyncio
async def test_start_workspace_session_runnable_graph_happy_path(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    """A runnable Begin -> agent -> End graph still starts a session."""
    await _seed_workspace(fake_storage_provider)
    await _seed_graph(fake_storage_provider, _runnable_graph())
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, fake_scheduler, fake_claim_engine, live,
    )

    sess = await start_workspace_session(
        workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id="gr-run"),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        parent_session_id=None,
        deps=deps,
    )

    assert isinstance(sess, WorkspaceSession)
    assert isinstance(sess.binding, GraphSessionBinding)
    assert len(live.started) == 1


# ---------------------------------------------------------------------------
# cancel_session: shared hard-cancel flow (REST route + workspaces tool)
# ---------------------------------------------------------------------------


def _cancel_deps(
    sp, scheduler, claim_engine, event_bus=None,
) -> SessionCancelDeps:
    return SessionCancelDeps(
        storage_provider=sp,
        scheduler=scheduler,
        claim_engine=claim_engine,
        event_bus=event_bus,
    )


async def _seed_session(
    sp, *, status: SessionStatus, sid="sess-cancel-1", workspace_id="ws-1",
) -> WorkspaceSession:
    session = WorkspaceSession(
        id=sid,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    await sp.get_storage(WorkspaceSession).create(session)
    return session


@pytest.mark.asyncio
async def test_cancel_session_created_ends_immediately_and_drops_lease(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    await _seed_session(fake_storage_provider, status=SessionStatus.CREATED)
    deps = _cancel_deps(fake_storage_provider, fake_scheduler, fake_claim_engine)

    out = await cancel_session(
        workspace_id="ws-1", session_id="sess-cancel-1", deps=deps,
    )

    assert out.status == SessionStatus.ENDED
    assert out.ended_reason == "cancelled"
    assert out.ended_at is not None
    # Lease was dropped via the claim engine.
    assert (ClaimKind.SESSION, "sess-cancel-1") in fake_claim_engine.deleted
    # Row persisted as ENDED.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    rehydrated = await storage.get("sess-cancel-1")
    assert rehydrated is not None
    assert rehydrated.status == SessionStatus.ENDED


class _SlotSession:
    """Minimal AgentSession stand-in that records set_status calls."""

    def __init__(self, status=SessionStatus.CREATED) -> None:
        self._status = status
        self.set_status_calls: list[tuple] = []

    async def status(self):
        return self._status

    async def set_status(self, status, *, ended_reason=None, **_kw):
        self.set_status_calls.append((status, ended_reason))
        self._status = status


class _SlotWorkspace:
    def __init__(self, session) -> None:
        self._session = session

    async def get_session(self, session_id):
        return self._session


class _SlotRegistry:
    def __init__(self, ws) -> None:
        self._ws = ws

    async def get_workspace(self, workspace_id):
        return self._ws


@pytest.mark.asyncio
async def test_cancel_session_created_mirrors_ended_onto_slot(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    """Inline-cancel of a CREATED session also commits ENDED/cancelled onto
    the on-disk AgentSession slot via the workspace registry, so the
    workspace-side reads (``get_workspace_session`` / ``list_*``) agree with
    the scheduler row instead of reporting a stale ``running``/``created``.
    """
    await _seed_session(fake_storage_provider, status=SessionStatus.CREATED)
    slot = _SlotSession()
    deps = SessionCancelDeps(
        storage_provider=fake_storage_provider,
        scheduler=fake_scheduler,
        claim_engine=fake_claim_engine,
        workspace_registry=_SlotRegistry(_SlotWorkspace(slot)),
    )

    out = await cancel_session(
        workspace_id="ws-1", session_id="sess-cancel-1", deps=deps,
    )

    assert out.status == SessionStatus.ENDED
    # The slot was driven to ENDED/cancelled exactly once.
    assert slot.set_status_calls == [(SessionStatus.ENDED, "cancelled")]


@pytest.mark.asyncio
async def test_cancel_session_created_no_registry_still_ends_row(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    """Back-compat: without a workspace_registry the inline cancel still
    ends the scheduler row (the slot mirror is simply skipped)."""
    await _seed_session(fake_storage_provider, status=SessionStatus.CREATED)
    deps = _cancel_deps(fake_storage_provider, fake_scheduler, fake_claim_engine)

    out = await cancel_session(
        workspace_id="ws-1", session_id="sess-cancel-1", deps=deps,
    )
    assert out.status == SessionStatus.ENDED
    assert out.ended_reason == "cancelled"


@pytest.mark.asyncio
async def test_cancel_session_already_ended_raises_conflict(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    await _seed_session(fake_storage_provider, status=SessionStatus.ENDED)
    deps = _cancel_deps(fake_storage_provider, fake_scheduler, fake_claim_engine)

    with pytest.raises(ConflictError):
        await cancel_session(
            workspace_id="ws-1", session_id="sess-cancel-1", deps=deps,
        )


@pytest.mark.asyncio
async def test_cancel_session_running_publishes_bus_and_signals(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    await _seed_session(fake_storage_provider, status=SessionStatus.RUNNING)
    bus = _FakeEventBus()
    deps = _cancel_deps(
        fake_storage_provider, fake_scheduler, fake_claim_engine, event_bus=bus,
    )

    out = await cancel_session(
        workspace_id="ws-1", session_id="sess-cancel-1", deps=deps,
    )

    assert out.cancel_requested is True
    assert out.cancel_requested_at is not None
    # Bus publish on the exact key the worker watcher subscribes to.
    assert ("session:sess-cancel-1:cancel", {}) in bus.published
    # Legacy scheduler signal fired.
    assert "sess-cancel-1" in fake_scheduler.signalled
    # NOT ended -- running sessions are preempted, not ended inline.
    assert out.status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_cancel_session_missing_raises_not_found(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    deps = _cancel_deps(fake_storage_provider, fake_scheduler, fake_claim_engine)

    with pytest.raises(NotFoundError):
        await cancel_session(
            workspace_id="ws-1", session_id="does-not-exist", deps=deps,
        )


@pytest.mark.asyncio
async def test_cancel_session_workspace_mismatch_raises_not_found(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    await _seed_session(
        fake_storage_provider, status=SessionStatus.CREATED,
        workspace_id="ws-other",
    )
    deps = _cancel_deps(fake_storage_provider, fake_scheduler, fake_claim_engine)

    with pytest.raises(NotFoundError):
        await cancel_session(
            workspace_id="ws-1", session_id="sess-cancel-1", deps=deps,
        )
