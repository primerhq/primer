"""REST surface for client attachment (S3 spec section 4)."""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


def _now() -> datetime:
    return datetime(2026, 8, 16, 10, 0, 0, tzinfo=timezone.utc)


async def _seed_session(fake_storage_provider, *, wid: str, sid: str, last_seq: int):
    row = WorkspaceSession(
        id=sid,
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.CREATED,
        created_at=_now(),
        last_seq=last_seq,
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(row)
    return row


async def test_attach_returns_the_mark_and_ttl(client, fake_storage_provider):
    await _seed_session(fake_storage_provider, wid="ws-a", sid="s-a", last_seq=12)
    r = await client.post(
        "/v1/workspaces/ws-a/sessions/s-a/attach", json={"client_id": "tab-a"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client_id"] == "tab-a"
    assert body["attached_seq"] == 12
    assert body["ttl_seconds"] == 30.0
    assert body["expires_at"]


async def test_heartbeat_keeps_the_original_mark(client, fake_storage_provider):
    row = await _seed_session(
        fake_storage_provider, wid="ws-b", sid="s-b", last_seq=3
    )
    first = await client.post(
        "/v1/workspaces/ws-b/sessions/s-b/attach", json={"client_id": "tab-a"}
    )
    assert first.json()["attached_seq"] == 3
    row.last_seq = 90
    await fake_storage_provider.get_storage(WorkspaceSession).update(row)
    second = await client.post(
        "/v1/workspaces/ws-b/sessions/s-b/attach", json={"client_id": "tab-a"}
    )
    assert second.json()["attached_seq"] == 3


async def test_attach_404s_on_a_foreign_session(client, fake_storage_provider):
    await _seed_session(fake_storage_provider, wid="ws-c", sid="s-c", last_seq=0)
    r = await client.post(
        "/v1/workspaces/other/sessions/s-c/attach", json={"client_id": "tab-a"}
    )
    assert r.status_code == 404, r.text


async def test_detach_is_idempotent(client, fake_storage_provider):
    await _seed_session(fake_storage_provider, wid="ws-d", sid="s-d", last_seq=0)
    await client.post(
        "/v1/workspaces/ws-d/sessions/s-d/attach", json={"client_id": "tab-a"}
    )
    first = await client.delete(
        "/v1/workspaces/ws-d/sessions/s-d/attach", params={"client_id": "tab-a"}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"detached": True}
    second = await client.delete(
        "/v1/workspaces/ws-d/sessions/s-d/attach", params={"client_id": "tab-a"}
    )
    assert second.json() == {"detached": False}
