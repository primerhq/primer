"""CRUD for /v1/channel_providers."""

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
async def test_create_channel_and_delete_provider(client):
    """ChannelProvider deletes cleanly once its child Channels are gone."""
    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-cas", "provider": "telegram",
              "config": {"bot_token": "123456:abcdefghijklmnopqrstuvwxyz123456"}},
    )
    r = await client.post(
        "/v1/channels",
        json={"id": "ch-cas", "provider_id": "cp-cas", "provider": "telegram",
              "external_id": "12345"},
    )
    assert r.status_code == 201, r.text
    # Delete the channel first then the provider
    r = await client.delete("/v1/channels/ch-cas")
    assert r.status_code in (200, 204)
    r = await client.delete("/v1/channel_providers/cp-cas")
    assert r.status_code in (200, 204)


@pytest.mark.asyncio
async def test_channel_provider_delete_blocked_by_channel(client):
    """ChannelProvider DELETE must 409 while a Channel references it.

    Regression guard for the §3 cascade-block (ReferenceCheck on
    ``Channel.provider_id``) that was collaterally dropped in commit
    ddb91310 when the WorkspaceChannelAssociation routers were removed.
    """
    r = await client.post(
        "/v1/channel_providers",
        json={"id": "cp-block", "provider": "telegram",
              "config": {"bot_token": "123456:abcdefghijklmnopqrstuvwxyz123456"}},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/channels",
        json={"id": "ch-block", "provider_id": "cp-block", "provider": "telegram",
              "external_id": "999"},
    )
    assert r.status_code == 201, r.text

    # Deleting the provider while the channel references it is blocked.
    r = await client.delete("/v1/channel_providers/cp-block")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["status"] == 409, body
    assert body["type"].endswith("/conflict"), body
    # Detail names the blocking child kind and the first referencing id.
    assert "ch-block" in body.get("detail", ""), body

    # Remove the channel first, then the provider deletes cleanly.
    r = await client.delete("/v1/channels/ch-block")
    assert r.status_code in (200, 204), r.text
    r = await client.delete("/v1/channel_providers/cp-block")
    assert r.status_code in (200, 204), r.text
