"""A channel's ``provider`` must be the platform of the ChannelProvider it names, on create AND on update (ticket 01a1139e).

docs/agents/channels.md said "``provider`` must match the referenced provider's platform" but nothing enforced it: through the REST
route a slack channel under a discord provider, and a telegram one under a slack provider, were stored with a 201. REST now answers 422
naming both platforms and nothing is stored or changed; the system tool's tests are in ``tests/toolset/test_system_validators.py``.

Two REST PUT behaviour changes are pinned here (the lead's ruling): a PUT that moves a channel to another platform than its provider is
refused, and so is a PUT that names a provider that does not exist (today the route checks neither on update).
"""

from __future__ import annotations

import pytest


async def _provider(client, provider_id: str, platform: str) -> None:
    config = {
        "slack": {"app_token": "xapp-test", "bot_token": "xoxb-test"},
        "discord": {"bot_token": "x" * 60},
        "telegram": {"bot_token": "123456789:ABCDEFGHIJKLMNOPQRST"},
    }[platform]
    r = await client.post("/v1/channel_providers", json={"id": provider_id, "provider": platform, "config": config})
    assert r.status_code == 201, r.text


def _channel(channel_id: str, provider_id: str, platform: str, external_id: str = "C1") -> dict:
    return {"id": channel_id, "provider_id": provider_id, "provider": platform, "external_id": external_id}


class TestCreate:
    @pytest.mark.asyncio
    async def test_a_slack_channel_under_a_discord_provider_is_refused(self, client) -> None:
        await _provider(client, "cp-discord", "discord")

        r = await client.post("/v1/channels", json=_channel("ch-bad", "cp-discord", "slack"))

        assert r.status_code == 422, r.text
        assert "slack" in r.json()["detail"] and "discord" in r.json()["detail"] and "cp-discord" in r.json()["detail"]
        assert (await client.get("/v1/channels/ch-bad")).status_code == 404, "a mismatched channel was stored"

    @pytest.mark.asyncio
    async def test_a_telegram_channel_under_a_slack_provider_is_refused(self, client) -> None:
        await _provider(client, "cp-slack", "slack")

        r = await client.post("/v1/channels", json=_channel("ch-bad", "cp-slack", "telegram"))

        assert r.status_code == 422, r.text

    @pytest.mark.asyncio
    async def test_a_matching_channel_is_created_as_before(self, client) -> None:
        await _provider(client, "cp-slack", "slack")

        r = await client.post("/v1/channels", json=_channel("ch-ok", "cp-slack", "slack"))

        assert r.status_code == 201, r.text


class TestUpdate:
    @pytest.mark.asyncio
    async def test_an_update_that_moves_a_channel_to_another_platform_is_refused(self, client) -> None:
        await _provider(client, "cp-slack", "slack")
        await _provider(client, "cp-discord", "discord")
        await client.post("/v1/channels", json=_channel("ch-1", "cp-slack", "slack"))

        r = await client.put("/v1/channels/ch-1", json=_channel("ch-1", "cp-discord", "slack"))

        assert r.status_code == 422, r.text
        stored = (await client.get("/v1/channels/ch-1")).json()
        assert stored["provider_id"] == "cp-slack", "the refused update changed the row"

    @pytest.mark.asyncio
    async def test_an_update_naming_a_missing_provider_is_refused(self, client) -> None:
        await _provider(client, "cp-slack", "slack")
        await client.post("/v1/channels", json=_channel("ch-1", "cp-slack", "slack"))

        r = await client.put("/v1/channels/ch-1", json=_channel("ch-1", "cp-ghost", "slack"))

        assert r.status_code == 422, r.text
        assert (await client.get("/v1/channels/ch-1")).json()["provider_id"] == "cp-slack"

    @pytest.mark.asyncio
    async def test_an_update_that_keeps_the_pair_is_accepted_as_before(self, client) -> None:
        await _provider(client, "cp-slack", "slack")
        await client.post("/v1/channels", json=_channel("ch-1", "cp-slack", "slack"))
        body = _channel("ch-1", "cp-slack", "slack")
        body["label"] = "renamed"

        r = await client.put("/v1/channels/ch-1", json=body)

        assert r.status_code == 200, r.text
        assert r.json()["label"] == "renamed"
