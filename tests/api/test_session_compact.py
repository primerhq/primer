"""REST tests for POST /v1/workspaces/{wid}/sessions/{sid}/compact.

S1 P2 Task 12. The guards are what these cover: the happy path needs a
real provider, which belongs in e2e rather than here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest


def _now() -> datetime:
    return datetime(2026, 8, 16, 10, 0, 0, tzinfo=UTC)


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: str) -> None:
        self._files[path] = content.encode("utf-8")

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f"{self.state_path}/sessions/{session_id}/messages.jsonl"
        self._files[path] = self._files.get(path, b"") + line


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-16T00:00:00+00:00"})


_LOG = "\n".join([
    _rec(1, "user_input", text="hello"),
    _rec(2, "done"),
]) + "\n"


async def _seed(fake_storage_provider, sid, binding=None, **over):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    fields = {
        "id": sid, "workspace_id": "ws-1",
        "binding": binding or AgentSessionBinding(agent_id="ag1"),
        "status": SessionStatus.WAITING, "created_at": _now(),
        "turn_status": "idle", "last_seq": 2,
    }
    fields.update(over)
    await fake_storage_provider.get_storage(WorkspaceSession).create(
        WorkspaceSession(**fields)
    )


def _wire(app, ws):
    async def _get(wid):
        return ws if wid == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_running_turn_is_409(client, app, fake_storage_provider):
    await _seed(fake_storage_provider, "c-1", turn_status="running")
    ws = _FakeWorkspace()
    ws.write(".state/sessions/c-1/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post("/v1/workspaces/ws-1/sessions/c-1/compact")
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_parked_session_is_409(client, app, fake_storage_provider):
    """A park is mid-turn: its resume still needs the folded history."""
    await _seed(fake_storage_provider, "c-2", parked_status="parked")
    ws = _FakeWorkspace()
    ws.write(".state/sessions/c-2/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post("/v1/workspaces/ws-1/sessions/c-2/compact")
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_graph_binding_is_409(client, app, fake_storage_provider):
    """Graph internals see graph state, not session history."""
    from primer.model.workspace_session import GraphSessionBinding

    await _seed(
        fake_storage_provider, "c-3",
        binding=GraphSessionBinding(graph_id="g1"),
    )
    ws = _FakeWorkspace()
    ws.write(".state/sessions/c-3/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post("/v1/workspaces/ws-1/sessions/c-3/compact")
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_unknown_session_is_404(client, app, fake_storage_provider):
    ws = _FakeWorkspace()
    _wire(app, ws)
    r = await client.post("/v1/workspaces/ws-1/sessions/nope/compact")
    assert r.status_code == 404, r.text
