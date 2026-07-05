"""REST session-create attribution — Layer 3 Task 2 (spec §8.1).

``POST /v1/workspaces/{workspace_id}/sessions`` must stamp the created
row's ``initiated_by`` from the authenticated caller
(``request.state.actor``, Layer 1's ``AuthMiddleware``), falling back to
the reserved system principal when the request carries no actor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.model.agent import Agent, AgentModel
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import WorkspaceSession


class _FakeWorkspace:
    """Enough of the ``Workspace`` surface for the agent-binding create path.

    Mirrors ``tests/trigger/conftest.py``'s ``_FakeWorkspace`` — the REST
    handler's ``start_workspace_session`` call drives
    ``live_workspace.start_session(...)`` to allocate the on-disk slot; this
    double just records the call so the fake registry can hand out a live
    instance without touching a real backend.
    """

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
        self.started_slots.append({"id": id, "binding": binding})


class _FakeWorkspaceRegistry:
    """Hands out a per-id ``_FakeWorkspace``, installed on ``app.state``."""

    def __init__(self) -> None:
        self.workspaces: dict[str, _FakeWorkspace] = {}

    async def get_workspace(self, workspace_id: str) -> _FakeWorkspace:
        return self.workspaces.setdefault(workspace_id, _FakeWorkspace(workspace_id))


async def _seed_workspace_and_agent(app) -> tuple[Workspace, Agent]:
    sp = app.state.storage_provider
    ws = Workspace(
        id="ws-attr-1",
        description="attribution test workspace",
        template_id="tpl-1",
        provider_id="p-1",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959/", token=SecretStr("t"),
        ),
    )
    await sp.get_storage(Workspace).create(ws)
    agent = Agent(
        id="ag-attr-1",
        description="attribution test agent",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    await sp.get_storage(Agent).create(agent)
    return ws, agent


@pytest.mark.asyncio
async def test_create_session_stamps_user_initiated_by(client, app) -> None:
    """A logged-in user's session create stamps ``initiated_by`` from them."""
    app.state.workspace_registry = _FakeWorkspaceRegistry()
    ws, agent = await _seed_workspace_and_agent(app)

    reg = await client.post(
        "/v1/auth/register",
        json={"username": "attruser", "password": "attrpassword"},
    )
    assert reg.status_code == 200, reg.text

    users = app.state.storage_provider.get_storage(User)
    user_row = next(
        u for u in users._data.values() if u.username == "attruser"  # noqa: SLF001
    )

    resp = await client.post(
        f"/v1/workspaces/{ws.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": agent.id}},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    sessions = app.state.storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(sid)
    assert row is not None
    assert row.initiated_by is not None
    assert row.initiated_by.type == "user"
    assert row.initiated_by.id == user_row.id
    assert row.initiated_by.source in {"local", "internal"}


@pytest.mark.asyncio
async def test_create_session_falls_back_to_system_when_unauthenticated(
    client, app,
) -> None:
    """No real actor on the request -> ``initiated_by`` is ``system``.

    With auth *enabled* (the default), an unauthenticated request never
    reaches the handler at all -- ``require_auth`` 401s first. The only
    way a real request reaches ``create_session`` with no genuine user
    actor is auth-disabled mode, where ``AuthMiddleware`` stamps a
    synthetic system ``Principal`` onto ``request.state.actor`` -- this
    exercises the same ``PrincipalRef`` projection code path the
    ``actor is None`` fallback covers.
    """
    app.state.workspace_registry = _FakeWorkspaceRegistry()
    app.state.config.auth.enabled = False
    ws, agent = await _seed_workspace_and_agent(app)

    resp = await client.post(
        f"/v1/workspaces/{ws.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": agent.id}},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    sessions = app.state.storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(sid)
    assert row is not None
    assert row.initiated_by is not None
    assert row.initiated_by.type == "system"
