"""CRUD + uniqueness + config round-trip for /v1/channels."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_channel_under_provider(client):
    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-x", "provider": "discord",
              "config": {"bot_token": "x" * 60}},
    )
    r = await client.post(
        "/v1/channels",
        json={"id": "ch-x", "provider_id": "cp-x", "provider": "discord",
              "external_id": "snowflake-1"},
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_create_channel_with_explicit_provider(client):
    """POST a channel body that includes provider; 201 returned and provider persisted."""
    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-infer", "provider": "slack",
              "config": {"app_token": "xapp-test", "bot_token": "xoxb-test"}},
    )
    r = await client.post(
        "/v1/channels",
        json={"id": "ch-infer", "provider_id": "cp-infer", "provider": "slack",
              "external_id": "C-INFER"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["provider"] == "slack"


@pytest.mark.asyncio
async def test_unique_provider_external_id_pair(client):
    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-u", "provider": "slack",
              "config": {"app_token": "xapp-test", "bot_token": "xoxb-test"}},
    )
    a = await client.post(
        "/v1/channels",
        json={"id": "ch-u1", "provider_id": "cp-u", "provider": "slack",
              "external_id": "C0001"},
    )
    assert a.status_code == 201
    b = await client.post(
        "/v1/channels",
        json={"id": "ch-u2", "provider_id": "cp-u", "provider": "slack",
              "external_id": "C0001"},
    )
    assert b.status_code == 409


@pytest.mark.asyncio
async def test_create_channel_with_chats_config_and_get_shows_config(client):
    """T12: POST a channel with config.chats enabled; GET returns the config."""
    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-cfg", "provider": "slack",
              "config": {"app_token": "xapp-test", "bot_token": "xoxb-test"}},
    )
    # Also seed the referenced agent so validation is happy (agent id is just a string)
    r = await client.post(
        "/v1/channels",
        json={
            "id": "ch-cfg",
            "provider_id": "cp-cfg",
            "provider": "slack",
            "external_id": "C-cfg",
            "config": {
                "chats": {
                    "enabled": True,
                    "default_agent": "agent-x",
                }
            },
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["config"]["chats"]["enabled"] is True
    assert created["config"]["chats"]["default_agent"] == "agent-x"

    r2 = await client.get("/v1/channels/ch-cfg")
    assert r2.status_code == 200, r2.text
    fetched = r2.json()
    assert fetched["config"]["chats"]["enabled"] is True
    assert fetched["config"]["chats"]["default_agent"] == "agent-x"
