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


# ---- An edit that leaves the secrets alone keeps them (audit G2-01) ------------------------------------------------------------
#
# GET serves every token as the mask, and the console's edit form sends that mask back for a secret left blank. The token
# validators used to refuse the mask's shape before the stored token could be restored, so every edit of a channel provider
# answered 422 unless the operator typed every secret again. Each case reads the row the router serves, changes one other field,
# and PUTs the served body back.

SLACK_APP = "xapp-1-A0123-live-app-token"
SLACK_BOT = "xoxb-0123-live-bot-token"
TELEGRAM_BOT = "123456:abcdefghijklmnopqrstuvwxyz123456"
DISCORD_BOT = "D" * 40

# platform -> (stored config, the one change the PUT makes, the secret fields and their stored values)
_EDITS = {
    "slack": (
        {"app_token": SLACK_APP, "bot_token": SLACK_BOT, "signing_secret": "old-signing-secret"},
        {"signing_secret": "rotated-signing-secret"},
        {"app_token": SLACK_APP, "bot_token": SLACK_BOT},
    ),
    "telegram": ({"bot_token": TELEGRAM_BOT, "poll_timeout_seconds": 25}, {"poll_timeout_seconds": 40}, {"bot_token": TELEGRAM_BOT}),
    "discord": ({"bot_token": DISCORD_BOT, "enable_dms": True}, {"enable_dms": False}, {"bot_token": DISCORD_BOT}),
}


async def _stored_config(fake_storage_provider, rid: str):
    from primer.model.channel import ChannelProvider

    row = await fake_storage_provider.get_storage(ChannelProvider).get(rid)
    assert row is not None
    return row.config


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", sorted(_EDITS))
async def test_an_edit_that_sends_the_served_masks_back_keeps_the_stored_tokens(client, fake_storage_provider, platform: str) -> None:
    from primer.common.url_userinfo import MASK

    stored, change, secrets = _EDITS[platform]
    rid = f"cp-edit-{platform}"
    r = await client.post("/v1/channel_providers", json={"id": rid, "provider": platform, "config": stored})
    assert r.status_code == 201, r.text
    served = (await client.get(f"/v1/channel_providers/{rid}")).json()
    assert {name: served["config"][name] for name in secrets} == {name: MASK for name in secrets}, "precondition: the tokens are served as the mask"

    body = {**served, "config": {**served["config"], **change}}
    r = await client.put(f"/v1/channel_providers/{rid}", json=body)

    assert r.status_code == 200, r.text
    config = await _stored_config(fake_storage_provider, rid)
    assert {name: getattr(config, name).get_secret_value() for name in secrets} == secrets
    for name, value in change.items():
        got = getattr(config, name)
        assert (got.get_secret_value() if hasattr(got, "get_secret_value") else got) == value


@pytest.mark.asyncio
async def test_a_mask_with_nothing_stored_to_restore_is_refused_and_never_stored(client, fake_storage_provider) -> None:
    """The optional Slack signing secret was never set, so the mask sent for it cannot be restored: it is refused, not stored."""
    from primer.common.url_userinfo import MASK

    r = await client.post("/v1/channel_providers", json={"id": "cp-nosign", "provider": "slack", "config": {"app_token": SLACK_APP, "bot_token": SLACK_BOT}})
    assert r.status_code == 201, r.text
    served = (await client.get("/v1/channel_providers/cp-nosign")).json()
    assert served["config"]["signing_secret"] is None

    r = await client.put("/v1/channel_providers/cp-nosign", json={**served, "config": {**served["config"], "signing_secret": MASK}})

    assert r.status_code == 422, r.text
    assert SLACK_APP not in r.text and SLACK_BOT not in r.text
    config = await _stored_config(fake_storage_provider, "cp-nosign")
    assert config.signing_secret is None
    assert config.app_token.get_secret_value() == SLACK_APP


@pytest.mark.asyncio
async def test_a_mask_sent_for_another_platform_is_refused(client, fake_storage_provider) -> None:
    """A PUT that moves the row to another platform has no stored token of that shape to restore the mask from."""
    from primer.common.url_userinfo import MASK

    r = await client.post("/v1/channel_providers", json={"id": "cp-move", "provider": "telegram", "config": {"bot_token": TELEGRAM_BOT}})
    assert r.status_code == 201, r.text

    r = await client.put("/v1/channel_providers/cp-move", json={"id": "cp-move", "provider": "discord", "config": {"bot_token": MASK}})

    assert r.status_code == 422, r.text
    assert (await _stored_config(fake_storage_provider, "cp-move")).bot_token.get_secret_value() == TELEGRAM_BOT


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", sorted(_EDITS))
async def test_a_create_that_carries_the_mask_is_refused(client, fake_storage_provider, platform: str) -> None:
    """A create has nothing stored to restore a mask from: the model lets the mask through, the route refuses it."""
    from primer.common.url_userinfo import MASK
    from primer.model.channel import ChannelProvider

    config = {**_EDITS[platform][0], **{name: MASK for name in _EDITS[platform][2]}}
    r = await client.post("/v1/channel_providers", json={"id": f"cp-new-{platform}", "provider": platform, "config": config})

    assert r.status_code == 422, r.text
    assert await fake_storage_provider.get_storage(ChannelProvider).get(f"cp-new-{platform}") is None


@pytest.mark.asyncio
async def test_a_token_that_breaks_its_rule_is_still_refused_on_an_edit(client, fake_storage_provider) -> None:
    r = await client.post("/v1/channel_providers", json={"id": "cp-bad", "provider": "slack", "config": {"app_token": SLACK_APP, "bot_token": SLACK_BOT}})
    assert r.status_code == 201, r.text

    r = await client.put("/v1/channel_providers/cp-bad", json={"id": "cp-bad", "provider": "slack", "config": {"app_token": SLACK_APP, "bot_token": "not-a-bot-token"}})

    assert r.status_code == 422, r.text
    assert (await _stored_config(fake_storage_provider, "cp-bad")).bot_token.get_secret_value() == SLACK_BOT
