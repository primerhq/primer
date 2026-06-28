"""REST tests for GET /v1/sessions/{id}/messages — the recorded message log.

Mirrors test_node_states_route.py: a fake workspace exposes read_file +
state_path. The headline behaviour is that an ENDED session still returns
its recorded history (unlike the WS, which rejects ended sessions)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


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


async def _seed_session(fake_storage_provider, sid: str, status):
    from primer.model.workspace_session import (
        AgentSessionBinding, SessionStatus, WorkspaceSession,
    )
    sess = WorkspaceSession(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status, created_at=_now(), turn_status="idle",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)


@pytest.mark.asyncio
async def test_ended_session_still_returns_history(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-ended", SessionStatus.ENDED)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-ended/messages.jsonl",
        '{"seq":1,"kind":"assistant_token","payload":{"text":"hi"}}\n'
        '{"seq":2,"kind":"done","payload":{}}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-ended/messages")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [it["seq"] for it in items] == [1, 2]


@pytest.mark.asyncio
async def test_after_seq_filters(client, app, fake_storage_provider):
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-run", SessionStatus.RUNNING)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-run/messages.jsonl",
        '{"seq":1,"kind":"a"}\n{"seq":2,"kind":"b"}\n{"seq":3,"kind":"c"}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-run/messages?after_seq=1")
    assert r.status_code == 200, r.text
    assert [it["seq"] for it in r.json()["items"]] == [2, 3]


@pytest.mark.asyncio
async def test_missing_file_is_empty_not_500(client, app, fake_storage_provider):
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-empty", SessionStatus.RUNNING)
    ws = _FakeWorkspace()  # no messages.jsonl written

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-empty/messages")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_unknown_session_404(client, app, fake_storage_provider):
    r = await client.get("/v1/sessions/nope/messages")
    assert r.status_code == 404
