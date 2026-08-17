"""REST tests for POST /v1/workspaces/{wid}/sessions/{sid}/binding.

S1 P3 Task 18. A session's agent stops being a create-time fact: an
idle session switches immediately, a busy one queues the request so the
running turn finishes under the binding it started with.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest


def _now() -> datetime:
    return datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def read(self, path: str) -> str:
        return self._files.get(path, b"").decode("utf-8")

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f"{self.state_path}/sessions/{session_id}/messages.jsonl"
        self._files[path] = self._files.get(path, b"") + line


async def _seed_agents(sp) -> None:
    from primer.model.agent import Agent, AgentModel

    for aid in ("agent-a", "agent-b"):
        if await sp.get_storage(Agent).get(aid) is None:
            await sp.get_storage(Agent).create(
                Agent(id=aid, description=aid,
                      model=AgentModel(profile_id="p--m"), tools=[],
                      system_prompt=[]),
            )


async def _seed(app, sid: str, **over):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    sp = app.state.storage_provider
    await _seed_agents(sp)
    fields = {
        "id": sid, "workspace_id": "ws-1",
        "binding": AgentSessionBinding(agent_id="agent-a"),
        "status": SessionStatus.WAITING, "created_at": _now(),
        "turn_status": "idle", "last_seq": 3,
    }
    fields.update(over)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(**fields))

    ws = _FakeWorkspace()

    async def _get(wid):
        return ws if wid == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return ws


async def _row(app, sid):
    from primer.model.workspace_session import WorkspaceSession

    return await app.state.storage_provider.get_storage(
        WorkspaceSession
    ).get(sid)


@pytest.mark.asyncio
async def test_idle_session_switches_immediately(client, app):
    ws = await _seed(app, "b-idle")
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/b-idle/binding",
        json={"kind": "agent", "agent_id": "agent-b"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["binding"]["agent_id"] == "agent-b"
    assert r.json()["binding_epoch"] == 1

    marker = json.loads(
        ws.read(".state/sessions/b-idle/messages.jsonl").splitlines()[0]
    )
    assert marker["kind"] == "agent_marker"
    assert marker["payload"]["binding_epoch"] == 1


@pytest.mark.asyncio
async def test_busy_session_queues_the_switch(client, app):
    """The running turn keeps the binding it started with."""
    ws = await _seed(app, "b-busy", turn_status="running")
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/b-busy/binding",
        json={"kind": "agent", "agent_id": "agent-b"},
    )
    assert r.status_code == 200, r.text

    fresh = await _row(app, "b-busy")
    assert fresh.pending_binding_switch["agent_id"] == "agent-b"
    assert fresh.binding.agent_id == "agent-a"  # unchanged for now
    assert fresh.binding_epoch == 0
    assert ws.read(".state/sessions/b-busy/messages.jsonl") == ""


@pytest.mark.asyncio
async def test_profile_only_change_still_bumps_and_marks(client, app):
    """Same agent, new model, is still a binding change."""
    await _seed(app, "b-prof")
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/b-prof/binding",
        json={"kind": "agent", "agent_id": "agent-a", "profile_id": "p-2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["binding"]["profile_id"] == "p-2"
    assert body["binding_epoch"] == 1


@pytest.mark.asyncio
async def test_unknown_agent_is_404_and_writes_nothing(client, app):
    ws = await _seed(app, "b-404")
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/b-404/binding",
        json={"kind": "agent", "agent_id": "nope"},
    )
    assert r.status_code == 404, r.text
    fresh = await _row(app, "b-404")
    assert fresh.binding.agent_id == "agent-a"
    assert fresh.binding_epoch == 0
    assert ws.read(".state/sessions/b-404/messages.jsonl") == ""


@pytest.mark.asyncio
async def test_ended_session_is_409(client, app):
    from primer.model.workspace_session import SessionStatus

    await _seed(app, "b-ended", status=SessionStatus.ENDED,
                ended_reason="completed")
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/b-ended/binding",
        json={"kind": "agent", "agent_id": "agent-b"},
    )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_parked_session_abandons_its_gate_then_switches(client, app):
    """The gate belongs to the OUTGOING agent, so waiting for it would
    deadlock exactly the situation switching exists to escape."""
    import json as _json

    ws = await _seed(
        app, "b-parked", parked_status="parked",
        parked_state={"tool_call_id": "tc-9", "mode": "ask_user"},
    )
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/b-parked/binding",
        json={"kind": "agent", "agent_id": "agent-b"},
    )
    assert r.status_code == 200, r.text

    fresh = await _row(app, "b-parked")
    assert fresh.binding.agent_id == "agent-b"
    assert fresh.parked_status is None
    assert fresh.parked_state is None

    blob = ws.read(".state/sessions/b-parked/messages.jsonl")
    kinds = [_json.loads(line)["kind"]
             for line in blob.splitlines() if line.strip()]
    # The gate is closed as rejected before the terminal, then the
    # hand-off is recorded.
    assert kinds == ["tool_result", "cancelled", "agent_marker"]


@pytest.mark.asyncio
async def test_kind_and_target_must_agree(client, app):
    await _seed(app, "b-bad")
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/b-bad/binding",
        json={"kind": "graph", "agent_id": "agent-b"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_unknown_session_is_404(client, app):
    await _seed(app, "b-any")
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/nope/binding",
        json={"kind": "agent", "agent_id": "agent-b"},
    )
    assert r.status_code == 404, r.text
