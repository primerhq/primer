"""REST tests for POST /v1/workspaces/{wid}/sessions/{sid}/restart.

Restart = reset-same-session (Task 6) + auto-wake (Task 3) chained in one
call (studio-agents-interact §5.3). Mirrors test_session_messages_route.py's
convention: monkeypatch ``app.state.workspace_registry.get_workspace`` with
a fake workspace exposing ``get_session`` (slot ``reopen``/``append_instruction``
for reset + wake) and ``append_message_line``/``read_file``/``state_path``
(so the recorded invocation divider round-trips through
``GET /v1/sessions/{sid}/messages``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


class _FakeSlot:
    def __init__(self) -> None:
        self.reopened = False
        self.instructions: list[str] = []

    async def reopen(self) -> None:
        self.reopened = True

    async def append_instruction(
        self, instruction: str, *, extra_parts=None,
    ) -> None:
        self.instructions.append(instruction)


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self.slots: dict[str, _FakeSlot] = {}

    async def get_session(self, session_id: str) -> _FakeSlot:
        return self.slots.setdefault(session_id, _FakeSlot())

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f"{self.state_path}/sessions/{session_id}/messages.jsonl"
        self._files[path] = self._files.get(path, b"") + line

    async def read_file(self, path: str) -> bytes:
        return self._files.get(path, b"")


@dataclass
class _Ctx:
    workspace_id: str
    session_id: str


async def _seed_session(
    fake_storage_provider, *, sid: str, wid: str, status, ended_reason=None,
):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id=sid,
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status,
        ended_reason=ended_reason,
        ended_at=_now() if status == SessionStatus.ENDED else None,
        last_seq=3,
        turn_status="idle",
        created_at=_now(),
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)
    return sess


@pytest.fixture
async def ended_session_client(client, app, fake_storage_provider):
    """A workspace with an ENDED/completed agent session."""
    from primer.model.workspace_session import SessionStatus

    wid, sid = "ws-restart", "s-ended"
    await _seed_session(
        fake_storage_provider,
        sid=sid,
        wid=wid,
        status=SessionStatus.ENDED,
        ended_reason="completed",
    )
    ws = _FakeWorkspace()

    async def _get(workspace_id: str):
        return ws if workspace_id == wid else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    yield client, _Ctx(workspace_id=wid, session_id=sid)


@pytest.fixture
async def active_session_client(client, app, fake_storage_provider):
    """A workspace with a RUNNING agent session."""
    from primer.model.workspace_session import SessionStatus

    wid, sid = "ws-restart-active", "s-running"
    await _seed_session(
        fake_storage_provider, sid=sid, wid=wid, status=SessionStatus.RUNNING,
    )
    ws = _FakeWorkspace()

    async def _get(workspace_id: str):
        return ws if workspace_id == wid else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    yield client, _Ctx(workspace_id=wid, session_id=sid)


async def test_restart_reopens_and_invokes(ended_session_client):
    """A completed agent session restarts: status -> running, divider written."""
    client, ctx = ended_session_client
    resp = await client.post(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/restart",
        json={"input": "go again"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["turn_status"] == "claimable"
    assert body["metadata"]["invocation"] == 2

    # The invocation divider is visible in the recorded message history.
    msgs = await client.get(f"/v1/sessions/{ctx.session_id}/messages")
    kinds = [m.get("kind") for m in msgs.json()["items"]]
    assert "invocation_divider" in kinds


async def test_restart_409_when_active(active_session_client):
    client, ctx = active_session_client
    resp = await client.post(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/restart",
        json={},
    )
    assert resp.status_code == 409

async def test_reopen_clears_the_stale_ended_detail_and_agent_phase(
    client, app, fake_storage_provider,
):
    """#718 carry-over: a reopen that leaves ended_detail or the agent_phase*
    fields behind labels the session's NEXT end with the previous turn's
    detail (the row is the one truth about how a session ended, and a reader
    that serves ended_detail shows the stale value the moment the next turn
    ends), and shows a phase the session never ran. The reopen already
    clears the terminal + park + turn bookkeeping of the same dead turn
    (turn_status, last_turn_error, the park fields) - the detail and the
    phase belong there too."""
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    wid, sid = "ws-restart", "s-stale-detail"
    sess = WorkspaceSession(
        id=sid,
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.ENDED,
        ended_reason="failed",
        ended_detail="tool_execution_failed",
        ended_at=_now(),
        last_seq=3,
        turn_status="idle",
        agent_phase="executing",
        agent_phase_turn_no=7,
        agent_phase_stamped_at=_now(),
        created_at=_now(),
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)
    ws = _FakeWorkspace()

    async def _get(workspace_id: str):
        return ws if workspace_id == wid else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    resp = await client.post(
        f"/v1/workspaces/{wid}/sessions/{sid}/restart",
        json={"input": "go again"},
    )
    assert resp.status_code == 200, resp.text

    row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
    assert row.ended_detail is None
    assert row.agent_phase is None
    assert row.agent_phase_turn_no is None
    assert row.agent_phase_stamped_at is None


async def test_a_persons_restart_reopens_a_session_an_operator_cancelled(client, app, fake_storage_provider):
    """Lead ruling 1 of the G7 fixes: no wake but a person's reopens a cancelled session, and the REST Restart is a person's. (The restart tool, an
    agent's or an MCP client's, is refused: ``tests/toolset/test_restart_tool_peer_guards.py``.)"""
    from primer.model.workspace_session import SessionStatus

    wid, sid = "ws-restart-cancelled", "s-cancelled"
    await _seed_session(fake_storage_provider, sid=sid, wid=wid, status=SessionStatus.ENDED, ended_reason="cancelled")
    ws = _FakeWorkspace()

    async def _get(workspace_id: str):
        return ws if workspace_id == wid else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    resp = await client.post(f"/v1/workspaces/{wid}/sessions/{sid}/restart", json={"input": "carry on"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["status"], body["turn_status"], body["ended_reason"]) == ("running", "claimable", None)
    assert ws.slots[sid].reopened is True and ws.slots[sid].instructions == ["carry on"]
