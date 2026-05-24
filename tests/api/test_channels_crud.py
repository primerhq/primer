"""CRUD + uniqueness + cascade-block for /v1/channels."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_channel_under_provider(client):
    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-x", "provider": "discord", "config": {}},
    )
    r = await client.post(
        "/v1/channels",
        json={"id": "ch-x", "provider_id": "cp-x", "external_id": "snowflake-1"},
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_unique_provider_external_id_pair(client):
    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-u", "provider": "slack",
              "config": {"app_token": "xapp-test", "bot_token": "xoxb-test"}},
    )
    a = await client.post(
        "/v1/channels",
        json={"id": "ch-u1", "provider_id": "cp-u", "external_id": "C0001"},
    )
    assert a.status_code == 201
    b = await client.post(
        "/v1/channels",
        json={"id": "ch-u2", "provider_id": "cp-u", "external_id": "C0001"},
    )
    assert b.status_code == 409
