"""API-level lifecycle emission: create + steer land on the event log.

Runs over the fake app harness (in-memory provider + event store), so
this pins the router/factory seams, not the SQL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.model.agent import Agent, AgentModel
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta


class _FakeWorkspace:
    def __init__(self, workspace_id: str) -> None:
        self.id = workspace_id

    async def start_session(
        self,
        binding: Any,
        *,
        id: str,
        instructions: Any = None,
        parent_session_id: Any = None,
        name: Any = None,
    ) -> None:
        return

    async def get_session(self, session_id: str) -> Any:
        # Slot absent is tolerated by the steer path (the USER_INPUT
        # record is still written through append_message_line).
        return None

    async def append_message_line(self, *args: Any, **kwargs: Any) -> None:
        return


class _FakeWorkspaceRegistry:
    async def get_workspace(self, workspace_id: str) -> _FakeWorkspace:
        return _FakeWorkspace(workspace_id)


async def _seed(app) -> tuple[Workspace, Agent]:
    sp = app.state.storage_provider
    ws = Workspace(
        id="ws-ev-1",
        description="event lifecycle test workspace",
        template_id="tpl-1",
        provider_id="p-1",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959/", token=SecretStr("t"),
        ),
    )
    await sp.get_storage(Workspace).create(ws)
    agent = Agent(
        id="ag-ev-1",
        description="event lifecycle test agent",
        model=AgentModel(profile_id="p--m"),
    )
    await sp.get_storage(Agent).create(agent)
    return ws, agent


@pytest.mark.asyncio
async def test_create_and_steer_land_on_the_event_log(client, app) -> None:
    app.state.workspace_registry = _FakeWorkspaceRegistry()
    ws, agent = await _seed(app)
    reg = await client.post(
        "/v1/auth/register",
        json={"username": "evuser", "password": "evpassword01"},
    )
    assert reg.status_code == 200, reg.text
    store = app.state.storage_provider.get_event_store()
    baseline = await store.max_id()

    resp = await client.post(
        f"/v1/workspaces/{ws.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": agent.id},
              "auto_start": False},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    steer = await client.post(
        f"/v1/workspaces/{ws.id}/sessions/{sid}/steer",
        json={"instruction": "hello"},
    )
    assert steer.status_code in (200, 201, 202), steer.text

    events = await store.read_after(baseline)
    types = [e.event_type for e in events]
    assert "session.invoked" in types, types
    assert "session.steered" in types, types

    invoked = next(e for e in events if e.event_type == "session.invoked")
    assert invoked.workspace_id == ws.id
    assert invoked.session_id == sid
    assert invoked.payload["binding"]["agent_id"] == agent.id

    steered = next(e for e in events if e.event_type == "session.steered")
    assert steered.session_id == sid
    assert steered.payload == {
        "has_instruction": True, "has_tool_results": False,
    }
