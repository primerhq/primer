"""A PUT cannot move a channel onto another channel's (provider_id, external_id) pair (ticket 01a11598-e1d7-7060-8fe9-d483dec4cf09).

Create refused a second channel on a pair (409) from the start, but the update paths never looked at the pair: a PUT moved a channel onto
another channel's pair and answered 200, leaving two rows on one pair. Inbound events are dispatched by ``external_id`` through a plain
per-connection dict that each adapter writes in ``initialize`` and unconditionally pops in ``aclose``, so two rows on one pair means the
last adapter to initialise wins and closing either one drops the other's route: not a state worth allowing.

This pins the REST PUT behaviour change: 409 conflict where it was 200. The row is unchanged on a refusal, a PUT that keeps its own pair
is accepted (the row does not conflict with itself), and the same external id under another provider stays allowed (the pair, not the
external id, is unique).
"""

from __future__ import annotations

import pytest


async def _provider(client, provider_id: str) -> None:
    r = await client.post(
        "/v1/channel_providers",
        json={"id": provider_id, "provider": "slack", "config": {"app_token": "xapp-test", "bot_token": "xoxb-test"}},
    )
    assert r.status_code == 201, r.text


def _channel(channel_id: str, provider_id: str, external_id: str, **extra) -> dict:
    return {"id": channel_id, "provider_id": provider_id, "provider": "slack", "external_id": external_id, **extra}


async def _create(client, body: dict) -> None:
    r = await client.post("/v1/channels", json=body)
    assert r.status_code == 201, r.text


class TestPutPair:
    @pytest.mark.asyncio
    async def test_a_put_onto_another_channels_pair_is_refused(self, client) -> None:
        await _provider(client, "cp-s")
        await _create(client, _channel("ch-1", "cp-s", "C1"))
        await _create(client, _channel("ch-2", "cp-s", "C2"))

        r = await client.put("/v1/channels/ch-2", json=_channel("ch-2", "cp-s", "C1"))

        assert r.status_code == 409, r.text
        assert "ch-1" in r.json()["detail"], "the refusal names the channel that holds the pair"
        assert (await client.get("/v1/channels/ch-2")).json()["external_id"] == "C2", "the refused PUT changed the row"

    @pytest.mark.asyncio
    async def test_a_put_that_moves_to_another_provider_but_onto_a_taken_pair_is_refused(self, client) -> None:
        await _provider(client, "cp-s")
        await _provider(client, "cp-t")
        await _create(client, _channel("ch-1", "cp-t", "C1"))
        await _create(client, _channel("ch-2", "cp-s", "C2"))

        r = await client.put("/v1/channels/ch-2", json=_channel("ch-2", "cp-t", "C1"))

        assert r.status_code == 409, r.text
        assert (await client.get("/v1/channels/ch-2")).json()["provider_id"] == "cp-s"

    @pytest.mark.asyncio
    async def test_a_put_that_keeps_its_own_pair_is_accepted(self, client) -> None:
        await _provider(client, "cp-s")
        await _create(client, _channel("ch-1", "cp-s", "C1"))

        r = await client.put("/v1/channels/ch-1", json=_channel("ch-1", "cp-s", "C1", label="renamed"))

        assert r.status_code == 200, r.text
        assert r.json()["label"] == "renamed"

    @pytest.mark.asyncio
    async def test_the_same_external_id_under_another_provider_is_allowed_on_a_put(self, client) -> None:
        await _provider(client, "cp-s")
        await _provider(client, "cp-t")
        await _create(client, _channel("ch-1", "cp-s", "C1"))
        await _create(client, _channel("ch-2", "cp-s", "C2"))

        r = await client.put("/v1/channels/ch-2", json=_channel("ch-2", "cp-t", "C1"))

        assert r.status_code == 200, r.text
        assert r.json()["provider_id"] == "cp-t"
