"""Unit tests for ``initiated_by`` threading through the session factory.

Spec §8.2: WorkspaceSession persists a PrincipalRef projection of the
actor that created it, so worker/scheduler resume can rehydrate
ctx.identity. Mirrors the fixture setup in test_session_factory.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.agent import Agent, AgentModel
from primer.model.principal import PrincipalRef
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, WorkspaceSession
from primer.workspace.session_factory import (
    SessionFactoryDeps,
    create_session,
    start_workspace_session,
)


class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)


class _FakeClaimEngine:
    def __init__(self) -> None:
        self.upserts: list[tuple] = []

    async def upsert(
        self, kind, entity_id: str, *, priority: int = 100, next_attempt_at=None,
    ) -> None:
        self.upserts.append((kind, entity_id, priority))


class _FakeLiveWorkspace:
    def __init__(self) -> None:
        self.started: list[dict] = []

    async def start_session(
        self, binding, *, id, instructions, parent_session_id, name=None
    ) -> None:
        self.started.append({"id": id})


class _FakeWorkspaceRegistry:
    def __init__(self, live: _FakeLiveWorkspace) -> None:
        self._live = live

    async def get_workspace(self, workspace_id: str) -> _FakeLiveWorkspace:
        return self._live


@pytest.fixture
def fake_scheduler() -> _FakeScheduler:
    return _FakeScheduler()


@pytest.fixture
def fake_claim_engine() -> _FakeClaimEngine:
    return _FakeClaimEngine()


@pytest.fixture
def deps(fake_storage_provider, fake_scheduler, fake_claim_engine) -> SessionFactoryDeps:
    return SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=None,
    )


def _binding() -> AgentSessionBinding:
    return AgentSessionBinding(agent_id="ag-1")


@pytest.mark.asyncio
async def test_create_session_persists_initiated_by(fake_storage_provider, deps):
    ref = PrincipalRef(
        type="user", id="user-1", display="alice", role="user", source="local",
    )
    sess = await create_session(
        workspace_id="ws-1",
        binding=_binding(),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        deps=deps,
        initiated_by=ref,
    )

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    rehydrated = await storage.get(sess.id)
    assert rehydrated is not None
    assert rehydrated.initiated_by is not None
    assert rehydrated.initiated_by.type == "user"
    assert rehydrated.initiated_by.id == "user-1"


@pytest.mark.asyncio
async def test_create_session_without_initiated_by_persists_none(
    fake_storage_provider, deps,
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

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    rehydrated = await storage.get(sess.id)
    assert rehydrated is not None
    assert rehydrated.initiated_by is None


@pytest.mark.asyncio
async def test_start_workspace_session_propagates_initiated_by(
    fake_storage_provider, fake_scheduler, fake_claim_engine,
):
    await fake_storage_provider.get_storage(WorkspaceRow).create(
        WorkspaceRow(
            id="ws-1",
            template_id="tpl-1",
            provider_id="local-1",
            created_at=datetime.now(timezone.utc),
            runtime_meta=WorkspaceRuntimeMeta(
                url="ws://127.0.0.1:5959/",
                token=SecretStr("t"),
            ),
        ),
    )
    await fake_storage_provider.get_storage(Agent).create(
        Agent(
            id="ag-1", description="x",
            model=AgentModel(provider_id="p", model_name="m"),
        ),
    )
    live = _FakeLiveWorkspace()
    full_deps = SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=_FakeWorkspaceRegistry(live),
    )

    sess = await start_workspace_session(
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        parent_session_id=None,
        deps=full_deps,
        initiated_by=PrincipalRef.system(),
    )

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    rehydrated = await storage.get(sess.id)
    assert rehydrated is not None
    assert rehydrated.initiated_by == PrincipalRef.system()
