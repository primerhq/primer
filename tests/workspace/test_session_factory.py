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
from primer.model.except_ import ConflictError, NotFoundError, ValidationError
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
    # Claim engine still receives a forward-compat upsert (matches router).
    assert any(k == ClaimKind.SESSION for k, _, _ in fake_claim_engine.upserts)


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
# start_workspace_session: the full create flow (validate + slot + persist)
# ---------------------------------------------------------------------------


class _FakeLiveWorkspace:
    """Records start_session calls; otherwise a no-op."""

    def __init__(self) -> None:
        self.started: list[dict] = []

    async def start_session(
        self, binding, *, id, instructions, parent_session_id
    ) -> None:
        self.started.append(
            {
                "binding": binding,
                "id": id,
                "instructions": instructions,
                "parent_session_id": parent_session_id,
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
