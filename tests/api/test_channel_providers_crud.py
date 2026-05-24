"""CRUD + cascade-block for /v1/channel_providers."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_and_list(client):
    r = await client.post(
        "/v1/channel_providers",
        json={"id": "cp-1", "provider": "slack",
              "config": {"app_token": "xapp-test", "bot_token": "xoxb-test"}},
    )
    assert r.status_code == 201, r.text
    r = await client.get("/v1/channel_providers")
    assert r.status_code == 200
    assert any(p["id"] == "cp-1" for p in r.json()["items"])


@pytest.mark.asyncio
async def test_delete_blocked_when_channel_references(client):
    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-cas", "provider": "telegram", "config": {}},
    )
    await client.post(
        "/v1/channels",
        json={"id": "ch-cas", "provider_id": "cp-cas", "external_id": "12345"},
    )
    r = await client.delete("/v1/channel_providers/cp-cas")
    assert r.status_code == 409, r.text
    r = await client.delete("/v1/channels/ch-cas")
    assert r.status_code in (200, 204)
    r = await client.delete("/v1/channel_providers/cp-cas")
    assert r.status_code in (200, 204)
