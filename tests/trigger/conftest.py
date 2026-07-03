"""Shared fixtures for trigger subscriber dispatcher tests.

Defines the small in-memory scheduler/claim-engine doubles plus
``seeded_workspace``/``seeded_agent``/``seeded_graph`` rows used by
``tests/trigger/test_subscribers_*.py``. The top-level
``fake_storage_provider`` fixture from ``tests/conftest.py`` provides
the storage backbone these fixtures populate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from primer.int.claim import ClaimKind


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeScheduler:
    """Captures every ``enqueue(sid)`` call for assertion."""

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)


class _FakeClaimEngine:
    """Captures every ``upsert(kind, id, priority)`` call."""

    def __init__(self) -> None:
        self.upserts: list[tuple[ClaimKind, str, int]] = []

    async def upsert(
        self,
        kind: ClaimKind,
        entity_id: str,
        *,
        priority: int = 100,
        next_attempt_at: Any = None,
    ) -> None:
        self.upserts.append((kind, entity_id, priority))


class _FakeWorkspace:
    """Records the on-disk session slot allocations the fresh-session
    dispatchers drive via ``start_session(binding, id=sid, ...)``.

    The real ``Workspace.start_session`` allocates the
    ``.state/sessions/<sid>/`` directory; the fresh-session dispatchers MUST
    drive it (via ``start_workspace_session``) so the worker's
    ``workspace.get_session(sid)`` can find the slot and actually run the
    agent/graph. The previous no-op double let the slot-allocation gap go
    unnoticed (the dispatcher used to call ``create_session`` directly and
    never allocated a slot); this double captures every allocation so the
    test can assert it happened."""

    def __init__(self, workspace_id: str) -> None:
        self.id = workspace_id
        self.started_slots: list[dict[str, Any]] = []

    async def start_session(
        self,
        binding: Any,
        *,
        id: str,
        instructions: Any = None,
        parent_session_id: Any = None,
        name: Any = None,
    ) -> None:
        self.started_slots.append(
            {
                "id": id,
                "binding": binding,
                "instructions": instructions,
                "parent_session_id": parent_session_id,
            }
        )


class _FakeWorkspaceRegistry:
    """Hands out a per-id :class:`_FakeWorkspace` and remembers it so the
    test can inspect which on-disk slots the dispatcher allocated."""

    def __init__(self) -> None:
        self.workspaces: dict[str, _FakeWorkspace] = {}

    async def get_workspace(self, workspace_id: str) -> _FakeWorkspace:
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            ws = _FakeWorkspace(workspace_id)
            self.workspaces[workspace_id] = ws
        return ws


class _FakeEventBus:
    """Captures every ``publish(key, payload)`` call."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
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
def fake_workspace_registry() -> _FakeWorkspaceRegistry:
    return _FakeWorkspaceRegistry()


@pytest.fixture
def fake_event_bus() -> _FakeEventBus:
    return _FakeEventBus()


@pytest.fixture
async def seeded_agent(fake_storage_provider):
    """Persist a minimal :class:`Agent` row and return it."""
    from primer.model.agent import Agent, AgentModel

    agent = Agent(
        id="ag-1",
        description="seeded test agent",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    await fake_storage_provider.get_storage(Agent).create(agent)
    return agent


@pytest.fixture
async def seeded_workspace(fake_storage_provider):
    """Persist a minimal :class:`Workspace` row and return it."""
    from pydantic import SecretStr

    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta

    ws = Workspace(
        id="ws-1",
        description="seeded test workspace",
        template_id="t-1",
        provider_id="p-1",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959/",
            token=SecretStr("t"),
        ),
    )
    await fake_storage_provider.get_storage(Workspace).create(ws)
    return ws


@pytest.fixture
async def seeded_graph(fake_storage_provider, seeded_agent):
    """Persist a minimal Begin → End :class:`Graph` row and return it."""
    from primer.model.graph import Graph, _BeginNode, _EndNode, _StaticEdge

    begin = _BeginNode(id="begin")
    end = _EndNode(id="end")
    graph = Graph(
        id="gr-1",
        description="seeded test graph",
        nodes=[begin, end],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    await fake_storage_provider.get_storage(Graph).create(graph)
    return graph
