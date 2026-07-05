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

    async def append_instruction(self, instruction: str) -> None:
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
