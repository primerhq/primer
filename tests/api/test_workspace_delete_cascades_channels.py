"""Deleting a workspace removes its channel_associations."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_workspace_delete_cascades(client, tmp_path):
    await client.post("/v1/channel_providers",
                      json={"id": "cp", "provider": "slack",
                            "config": {"app_token": "xapp-test", "bot_token": "xoxb-test"}})
    await client.post("/v1/channels",
                      json={"id": "ch", "provider_id": "cp", "external_id": "Cz"})
    await client.post("/v1/workspace_providers",
                      json={"id": "wp", "provider": "local",
                            "config": {"kind": "local", "root_path": str(tmp_path)}})
    await client.post("/v1/workspace_templates",
                      json={"id": "tpl", "description": "t", "provider_id": "wp",
                            "backend": {"kind": "local"}})
    ws = await client.post("/v1/workspaces", json={"template_id": "tpl"})
    wid = ws.json()["id"]
    await client.post(
        f"/v1/workspaces/{wid}/channel_associations",
        json={"id": "as", "workspace_id": wid, "channel_id": "ch"},
    )
    r = await client.get(f"/v1/workspace_channel_associations/as")
    assert r.status_code == 200

    r = await client.delete(f"/v1/workspaces/{wid}")
    assert r.status_code in (200, 204)

    r = await client.get(f"/v1/workspace_channel_associations/as")
    assert r.status_code == 404
