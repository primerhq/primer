"""CRUD + uniqueness + workspace-delete cascade for associations."""

from __future__ import annotations

import pytest


async def _seed_provider_channel_workspace(client, tmp_path=None):
    """Build a workspace + a channel that can be associated."""
    import tempfile, os
    ws_path = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    await client.post("/v1/channel_providers",
                      json={"id": "cp-a", "provider": "slack", "config": {}})
    await client.post("/v1/channels",
                      json={"id": "ch-a", "provider_id": "cp-a", "external_id": "Ca"})
    await client.post("/v1/workspace_providers",
                      json={"id": "wp-a", "provider": "local",
                            "config": {"kind": "local", "path": ws_path}})
    await client.post("/v1/workspace_templates",
                      json={"id": "tpl-a", "description": "t", "provider_id": "wp-a",
                            "backend": {"kind": "local"}})
    ws = await client.post("/v1/workspaces", json={"template_id": "tpl-a"})
    return ws.json()["id"]


@pytest.mark.asyncio
async def test_association_unique_per_workspace_channel_pair(client):
    wid = await _seed_provider_channel_workspace(client)
    r1 = await client.post(
        f"/v1/workspaces/{wid}/channel_associations",
        json={"id": "as-1", "workspace_id": wid, "channel_id": "ch-a"},
    )
    assert r1.status_code == 201
    r2 = await client.post(
        f"/v1/workspaces/{wid}/channel_associations",
        json={"id": "as-2", "workspace_id": wid, "channel_id": "ch-a"},
    )
    assert r2.status_code == 409
