"""REST tests for POST /v1/workspaces/{wid}/sessions/{sid}/rewind.

S1 P2 Task 11. Follows test_session_messages_route.py: a fake workspace
supplies read_file + state_path plus the WorkspaceIO append surface the
marker writer needs.

The status split is the point. Amendment C2's compaction case is a 409,
a state conflict the operator resolves by choosing a later target; a
malformed target is a 422.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest


def _now() -> datetime:
    return datetime(2026, 8, 16, 10, 0, 0, tzinfo=UTC)


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: str) -> None:
        self._files[path] = content.encode("utf-8")

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


def _rec(seq, kind, **payload):
    import json

    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-16T00:00:00+00:00"})


_LOG = "\n".join([
    _rec(1, "user_input", text="first"),
    _rec(2, "assistant_token", text="ok"),
    _rec(3, "done"),
    _rec(4, "user_input", text="second"),
    _rec(5, "done"),
]) + "\n"


async def _seed(fake_storage_provider, sid, **over):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    fields = {
        "id": sid, "workspace_id": "ws-1",
        "binding": AgentSessionBinding(agent_id="ag1"),
        "status": SessionStatus.WAITING, "created_at": _now(),
        "turn_status": "idle", "last_seq": 5,
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
async def test_rewind_appends_a_marker(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    await _seed(fake_storage_provider, "s-1")
    ws = _FakeWorkspace()
    ws.write(".state/sessions/s-1/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post(
        "/v1/workspaces/ws-1/sessions/s-1/rewind", json={"to_seq": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["to_seq"] == 1
    assert body["marker_seq"] == 6
    # Appended, never rewritten: the original rows are all still there.
    written = ws.read(".state/sessions/s-1/messages.jsonl")
    assert '"kind": "rewind_marker"' in written or "rewind_marker" in written
    assert written.count("user_input") == 2


@pytest.mark.asyncio
async def test_rewind_into_a_compacted_span_is_409(
    client, app, fake_storage_provider,
):
    await _seed(fake_storage_provider, "s-2", last_seq=7)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-2/messages.jsonl",
        _LOG
        + _rec(6, "compaction_marker", summary="so far", replaced_to_seq=5)
        + "\n"
        + _rec(7, "user_input", text="after")
        + "\n"
        + _rec(8, "done")
        + "\n",
    )
    _wire(app, ws)

    r = await client.post(
        "/v1/workspaces/ws-1/sessions/s-2/rewind", json={"to_seq": 1},
    )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_non_user_input_target_is_422(client, app, fake_storage_provider):
    await _seed(fake_storage_provider, "s-3")
    ws = _FakeWorkspace()
    ws.write(".state/sessions/s-3/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post(
        "/v1/workspaces/ws-1/sessions/s-3/rewind", json={"to_seq": 3},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_busy_session_is_409(client, app, fake_storage_provider):
    """Rewinding under a live turn would race the writer it is using."""
    await _seed(fake_storage_provider, "s-4", turn_status="running")
    ws = _FakeWorkspace()
    ws.write(".state/sessions/s-4/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post(
        "/v1/workspaces/ws-1/sessions/s-4/rewind", json={"to_seq": 1},
    )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_unknown_session_is_404(client, app, fake_storage_provider):
    ws = _FakeWorkspace()
    _wire(app, ws)
    r = await client.post(
        "/v1/workspaces/ws-1/sessions/nope/rewind", json={"to_seq": 1},
    )
    assert r.status_code == 404, r.text
