"""Tests for PUT/DELETE /v1/workspaces/{id}/channel_association."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pydantic import SecretStr

from primer.model.channel import Channel, ChannelProvider
from primer.model.workspace import Workspace, WorkspaceChannelLink, WorkspaceRuntimeMeta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(ws_id: str = "ws-t1") -> Workspace:
    """Minimal Workspace row for direct storage seeding."""
    return Workspace(
        id=ws_id,
        template_id="tpl-x",
        provider_id="wp-x",
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://localhost:5959",
            token=SecretStr("test-token"),
        ),
    )


async def _seed_channel(app, channel_id: str = "ch-t1") -> None:
    """Seed a ChannelProvider + Channel directly into storage."""
    sp = app.state.storage_provider
    cp = ChannelProvider(
        id="cp-t1",
        provider="slack",
        config={"app_token": "xapp-test", "bot_token": "xoxb-test"},
    )
    ch = Channel(
        id=channel_id,
        provider_id="cp-t1",
        provider="slack",
        external_id="C9999",
    )
    await sp.get_storage(ChannelProvider).create(cp)
    await sp.get_storage(Channel).create(ch)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_channel_association_sets_and_get_reflects(client, app):
    sp = app.state.storage_provider
    ws = _make_workspace()
    await sp.get_storage(Workspace).create(ws)
    await _seed_channel(app)

    r = await client.put(
        f"/v1/workspaces/{ws.id}/channel_association",
        json={"channel_id": "ch-t1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply_binding"]["channel_id"] == "ch-t1"

    # GET the workspace and confirm the binding is persisted
    r2 = await client.get(f"/v1/workspaces/{ws.id}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["reply_binding"]["channel_id"] == "ch-t1"


@pytest.mark.asyncio
async def test_delete_channel_association_clears_it(client, app):
    sp = app.state.storage_provider
    ws = _make_workspace("ws-t2")
    ws = ws.model_copy(
        update={"reply_binding": WorkspaceChannelLink(channel_id="ch-t2")}
    )
    await sp.get_storage(Workspace).create(ws)

    r = await client.delete(f"/v1/workspaces/{ws.id}/channel_association")
    assert r.status_code == 204, r.text

    r2 = await client.get(f"/v1/workspaces/{ws.id}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["reply_binding"] is None


@pytest.mark.asyncio
async def test_put_channel_association_nonexistent_channel_returns_4xx(client, app):
    sp = app.state.storage_provider
    ws = _make_workspace("ws-t3")
    await sp.get_storage(Workspace).create(ws)

    r = await client.put(
        f"/v1/workspaces/{ws.id}/channel_association",
        json={"channel_id": "does-not-exist"},
    )
    assert r.status_code in (404, 422), r.text


@pytest.mark.asyncio
async def test_put_channel_association_missing_workspace_returns_404(client):
    r = await client.put(
        "/v1/workspaces/no-such-ws/channel_association",
        json={"channel_id": "ch-t1"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_delete_channel_association_missing_workspace_returns_404(client):
    r = await client.delete(
        "/v1/workspaces/no-such-ws/channel_association"
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_put_channel_association_terminating_workspace_returns_409(client, app):
    sp = app.state.storage_provider
    ws = _make_workspace("ws-t4")
    ws = ws.model_copy(update={"phase": "terminating"})
    await sp.get_storage(Workspace).create(ws)
    await _seed_channel(app, channel_id="ch-t4")

    r = await client.put(
        f"/v1/workspaces/{ws.id}/channel_association",
        json={"channel_id": "ch-t4"},
    )
    assert r.status_code == 409, r.text
