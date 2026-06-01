"""Extracted ``create_session`` helper — Plan §3.2.

Mirrors the core persist + auto-start + claim-register semantics of
``POST /v1/workspaces/{wid}/sessions`` so the trigger dispatcher
(Phase 4+) can build fresh sessions without going through HTTP.
Spec §12.5.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind
from primer.model.agent import Agent, AgentModel
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.workspace.session_factory import (
    SessionFactoryDeps,
    create_session,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)


class _FakeClaimEngine:
    def __init__(self) -> None:
        self.upserts: list[tuple[ClaimKind, str, int]] = []

    async def upsert(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 100,
        next_attempt_at=None,
    ) -> None:
        self.upserts.append((kind, entity_id, priority))


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
