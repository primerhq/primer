"""Tests for the /v1/workspaces/{wid}/sessions REST surface.

Mirrors the conventions in ``tests/api/conftest.py`` (in-memory
``StorageProvider``) and ``tests/api/test_workers.py`` (in-memory
``Scheduler`` already attached by ``create_test_app``).

This module overrides the shared ``app`` fixture with one that wires
a fake :class:`WorkspaceRegistry` -- the create-session handler now
calls ``workspace.start_session(...)`` to allocate the on-disk slot,
and that requires a live backend.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from matrix.api.app import create_test_app
from matrix.api.registries import (
    ProviderRegistry,
    WorkspaceRegistry,
)


# ===========================================================================
# Fake workspace backend — auto-creates a _FakeWorkspace on first lookup
# ===========================================================================


class _FakeWorkspaceForSessions:
    """Just enough of the :class:`Workspace` surface for session tests.

    Tracks ``start_session`` calls so tests can assert the on-disk slot
    was allocated with the expected id.
    """

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.id = workspace_id
        self.started_sessions: dict[str, dict] = {}

    async def start_session(
        self,
        agent_binding,
        *,
        id=None,
        instructions=None,
        parent_session_id=None,
    ):
        if id is None:
            raise AssertionError(
                "REST API must always supply an explicit id to start_session"
            )
        if id in self.started_sessions:
            from matrix.model.except_ import ConflictError

            raise ConflictError(f"session {id!r} already exists")
        self.started_sessions[id] = {
            "agent_binding": agent_binding,
            "instructions": instructions,
            "parent_session_id": parent_session_id,
        }
        return object()  # AgentSession stand-in; the router ignores the return.

    async def aclose(self):
        return


class _FakeBackendForSessions:
    """Auto-instantiates a fake workspace handle on first ``get(wid)``.

    Mirrors what a real backend would do after a ``Workspace`` row is
    seeded directly via Storage but no ``materialise(...)`` ran.
    """

    def __init__(self, _provider) -> None:
        self._workspaces: dict[str, _FakeWorkspaceForSessions] = {}

    async def initialize(self):
        return

    async def aclose(self):
        for ws in self._workspaces.values():
            await ws.aclose()
        self._workspaces.clear()

    async def get(self, workspace_id, *, template=None):
        if workspace_id not in self._workspaces:
            self._workspaces[workspace_id] = _FakeWorkspaceForSessions(
                workspace_id
            )
        return self._workspaces[workspace_id]

    async def list(self):
        return list(self._workspaces.keys())


@pytest.fixture
def app(
    fake_storage_provider,
    fake_provider_registry,
) -> FastAPI:
    """Override the conftest ``app`` fixture with a fake WorkspaceRegistry.

    The session create handler now calls ``workspace.start_session(...)``
    via the registry; the default registry uses the real
    ``WorkspaceBackendFactory`` which would try to spin up a
    ``LocalWorkspaceBackend``. Inject an in-memory backend instead.
    """
    workspace_registry = WorkspaceRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        factory=_FakeBackendForSessions,
    )
    return create_test_app(
        storage_provider=fake_storage_provider,  # type: ignore[arg-type]
        provider_registry=fake_provider_registry,
        workspace_registry=workspace_registry,
    )


@pytest.fixture
async def sessions_client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        yield c


@pytest.fixture
async def seeded_workspace(app):
    """Insert a synthetic Workspace row + a matching WorkspaceProvider row.

    The provider row is required because the create-session handler
    resolves the live workspace via :class:`WorkspaceRegistry`, which
    looks up the backend by ``provider_id``.
    """
    from matrix.model.workspace import (
        LocalWorkspaceConfig,
        Workspace,
        WorkspaceProvider,
        WorkspaceProviderType,
    )

    sp = app.state.storage_provider
    provider_storage = sp.get_storage(WorkspaceProvider)
    await provider_storage.create(
        WorkspaceProvider(
            id="p-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(path="/tmp/matrix-ws-tests"),
        )
    )

    storage = sp.get_storage(Workspace)
    ws = Workspace(
        id="ws-test",
        template_id="t-1",
        provider_id="p-1",
        created_at=datetime.now(timezone.utc),
    )
    await storage.create(ws)
    yield ws
    try:
        await storage.delete(ws.id)
    except Exception:
        pass
    try:
        await provider_storage.delete("p-1")
    except Exception:
        pass


@pytest.fixture
async def seeded_agent(app):
    """Insert a synthetic Agent row directly via Storage[Agent]."""
    from matrix.model.agent import Agent, AgentModel

    storage = app.state.storage_provider.get_storage(Agent)
    agent = Agent(
        id="ag-test",
        description="test agent",
        model=AgentModel(provider_id="llm-p", model_name="m"),
        tools=[],
        system_prompt=[],
    )
    await storage.create(agent)
    yield agent
    try:
        await storage.delete(agent.id)
    except Exception:
        pass


async def test_create_session_default_status_is_created(
    sessions_client, seeded_workspace, seeded_agent,
):
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": seeded_agent.id},
            "auto_start": False,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "created"
    assert body["workspace_id"] == seeded_workspace.id
    assert body["binding"]["kind"] == "agent"
    assert body["turn_no"] == 0


async def test_create_session_with_auto_start_enqueues(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": seeded_agent.id},
            "auto_start": True,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["started_at"] is not None


async def test_create_session_unknown_workspace_404(
    sessions_client, seeded_agent,
):
    resp = await sessions_client.post(
        "/v1/workspaces/does-not-exist/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    assert resp.status_code == 404


async def test_create_session_unknown_agent_422(
    sessions_client, seeded_workspace,
):
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": "does-not-exist"}},
    )
    # Spec §11.4 step 2: binding-level validation failure → 422.
    assert resp.status_code == 422, resp.text


async def test_create_session_with_graph_binding(
    sessions_client, seeded_workspace, app,
):
    """Smoke test for the graph binding kind. Insert a synthetic Graph row."""
    from matrix.model.graph import Graph, _TerminalNode

    storage = app.state.storage_provider.get_storage(Graph)
    graph = Graph(
        id="gr-test",
        description="g",
        nodes=[_TerminalNode(id="end")],
        edges=[],
        entry_node_id="end",
    )
    await storage.create(graph)
    try:
        resp = await sessions_client.post(
            f"/v1/workspaces/{seeded_workspace.id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": "gr-test"},
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["binding"]["kind"] == "graph"
    finally:
        try:
            await storage.delete(graph.id)
        except Exception:
            pass


# ===========================================================================
# Task 20 — resume / pause / cancel + top-level list / get / find
# ===========================================================================


async def test_resume_from_created_transitions_to_running(
    sessions_client, seeded_workspace, seeded_agent,
):
    create = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    sid = create.json()["id"]
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}/resume"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["started_at"] is not None


async def test_resume_already_running_is_idempotent_200(
    sessions_client, seeded_workspace, seeded_agent,
):
    create = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": seeded_agent.id},
            "auto_start": True,
        },
    )
    sid = create.json()["id"]
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}/resume"
    )
    assert resp.status_code == 200


async def test_resume_ended_session_is_409(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    from matrix.model.session import Session
    create = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    sid = create.json()["id"]
    storage = app.state.storage_provider.get_storage(Session)
    s = await storage.get(sid)
    s.status = "ended"
    await storage.update(s)
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}/resume"
    )
    assert resp.status_code == 409


async def test_cancel_from_created_ends_immediately(
    sessions_client, seeded_workspace, seeded_agent,
):
    create = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    sid = create.json()["id"]
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}/cancel"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ended"
    assert body["ended_reason"] == "cancelled"


async def test_pause_running_sets_pause_requested_flag(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    from matrix.model.session import Session
    create = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": seeded_agent.id},
            "auto_start": True,
        },
    )
    sid = create.json()["id"]
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}/pause"
    )
    assert resp.status_code == 204
    storage = app.state.storage_provider.get_storage(Session)
    s = await storage.get(sid)
    assert s.pause_requested is True


async def test_top_level_list_sessions(
    sessions_client, seeded_workspace, seeded_agent,
):
    await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    resp = await sessions_client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert any(s["workspace_id"] == seeded_workspace.id for s in body["items"])


async def test_top_level_get_session_by_id(
    sessions_client, seeded_workspace, seeded_agent,
):
    create = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    sid = create.json()["id"]
    resp = await sessions_client.get(f"/v1/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == sid


async def test_top_level_get_unknown_session_404(sessions_client):
    resp = await sessions_client.get("/v1/sessions/does-not-exist")
    assert resp.status_code == 404


async def test_create_session_allocates_on_disk_slot(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    """The create handler must call ``Workspace.start_session(..., id=sid)``
    so the persisted Session row and the on-disk slot share the same id
    (spec §11.4 step 5).
    """
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    registry = app.state.workspace_registry
    live_workspace = await registry.get_workspace(seeded_workspace.id)
    assert sid in live_workspace.started_sessions
    on_disk = live_workspace.started_sessions[sid]
    assert on_disk["agent_binding"].agent_id == seeded_agent.id


async def test_create_session_graph_binding_skips_on_disk_slot(
    sessions_client, seeded_workspace, app,
):
    """Graph bindings must not allocate an on-disk slot (graph executor
    wires its own per-node session slots)."""
    from matrix.model.graph import Graph, _TerminalNode

    storage = app.state.storage_provider.get_storage(Graph)
    graph = Graph(
        id="gr-skip",
        description="g",
        nodes=[_TerminalNode(id="end")],
        edges=[],
        entry_node_id="end",
    )
    await storage.create(graph)
    try:
        resp = await sessions_client.post(
            f"/v1/workspaces/{seeded_workspace.id}/sessions",
            json={"binding": {"kind": "graph", "graph_id": "gr-skip"}},
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]

        registry = app.state.workspace_registry
        live_workspace = await registry.get_workspace(seeded_workspace.id)
        assert sid not in live_workspace.started_sessions
    finally:
        try:
            await storage.delete(graph.id)
        except Exception:
            pass


async def test_top_level_list_sessions_filtered_by_status(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    """Top-level GET /v1/sessions must honour ``?status=`` from §11.2."""
    from matrix.model.session import Session, SessionStatus

    # Two sessions: one CREATED (default), one auto-started → RUNNING.
    r1 = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    r2 = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": seeded_agent.id},
            "auto_start": True,
        },
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    sid_created = r1.json()["id"]
    sid_running = r2.json()["id"]

    resp = await sessions_client.get("/v1/sessions?status=running")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {s["id"] for s in body["items"]}
    assert sid_running in ids
    assert sid_created not in ids

    # Sanity: the unfiltered list returns both.
    resp_all = await sessions_client.get("/v1/sessions")
    assert resp_all.status_code == 200
    all_ids = {s["id"] for s in resp_all.json()["items"]}
    assert {sid_created, sid_running}.issubset(all_ids)

    # Make sure storage.find() is what's responding to the filter — verify
    # the session row's status is what the API claimed.
    storage = app.state.storage_provider.get_storage(Session)
    s = await storage.get(sid_running)
    assert s.status == SessionStatus.RUNNING


async def test_top_level_list_sessions_filtered_by_agent_id(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    """``?agent_id=`` filters by ``binding.agent_id`` (nested-JSON path)."""
    from matrix.model.agent import Agent, AgentModel

    # Insert a second agent so we have two distinct agent_id values.
    storage = app.state.storage_provider.get_storage(Agent)
    other = Agent(
        id="ag-other",
        description="other",
        model=AgentModel(provider_id="llm-p", model_name="m"),
        tools=[],
        system_prompt=[],
    )
    await storage.create(other)
    try:
        r1 = await sessions_client.post(
            f"/v1/workspaces/{seeded_workspace.id}/sessions",
            json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
        )
        r2 = await sessions_client.post(
            f"/v1/workspaces/{seeded_workspace.id}/sessions",
            json={"binding": {"kind": "agent", "agent_id": "ag-other"}},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        sid_a = r1.json()["id"]
        sid_b = r2.json()["id"]

        resp = await sessions_client.get(
            f"/v1/sessions?agent_id={seeded_agent.id}"
        )
        assert resp.status_code == 200, resp.text
        ids = {s["id"] for s in resp.json()["items"]}
        assert sid_a in ids
        assert sid_b not in ids
    finally:
        try:
            await storage.delete("ag-other")
        except Exception:
            pass
