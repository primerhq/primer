"""REST round-trip for a channel trigger + a matched subscription.

Creates a ``channel`` trigger, then a subscription carrying both an
``event_matcher`` and a ``reply_target``, and asserts they round-trip
through a follow-up GET.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.api._sub_targets import seed_sub_targets

@pytest_asyncio.fixture(autouse=True)
async def _sub_targets(app):
    await seed_sub_targets(app.state.storage_provider)



@pytest.mark.asyncio
async def test_create_channel_trigger_and_matched_subscription(client):
    # Create a channel trigger.
    r = await client.post(
        "/v1/triggers",
        json={
            "slug": "ch-trig",
            "name": "Channel trigger",
            "config": {"kind": "channel", "provider_id": "cp-1"},
        },
    )
    assert r.status_code == 201, r.text
    trig = r.json()
    trig_id = trig["id"]

    # Create a subscription with an event_matcher + reply_target.
    r2 = await client.post(
        f"/v1/triggers/{trig_id}/subscriptions",
        json={
            "config": {
                "kind": "agent_fresh_session",
                "workspace_id": "ws-1",
                "agent_id": "ag-1",
            },
            "event_matcher": {
                "event_type": "command.invoked",
                "command_name": "run",
            },
            "reply_target": "source_thread",
        },
    )
    assert r2.status_code == 201, r2.text
    sub = r2.json()
    sub_id = sub["id"]
    assert sub["event_matcher"]["event_type"] == "command.invoked"
    assert sub["event_matcher"]["command_name"] == "run"
    assert sub["reply_target"] == "source_thread"

    # GET the subscription and confirm both fields round-trip.
    r3 = await client.get(
        f"/v1/triggers/{trig_id}/subscriptions/{sub_id}"
    )
    assert r3.status_code == 200, r3.text
    got = r3.json()
    assert got["event_matcher"]["event_type"] == "command.invoked"
    assert got["event_matcher"]["command_name"] == "run"
    assert got["reply_target"] == "source_thread"


@pytest.mark.asyncio
async def test_dm_sender_is_refused_on_create_and_update_and_an_explicit_target_round_trips(client):
    """Ticket 01a16e37-b98e, ruling 2: no channel can post to a person's direct messages, so ``dm_sender`` is refused (422 naming
    ``reply_target``) and nothing is stored or changed. ``none`` and an explicit ``{channel_id, anchor}`` are kept as sent."""
    r = await client.post("/v1/triggers", json={"slug": "ch-dm", "name": "Channel", "config": {"kind": "channel", "provider_id": "cp-1"}})
    assert r.status_code == 201, r.text
    trig_id = r.json()["id"]
    config = {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "ag-1"}

    refused = await client.post(f"/v1/triggers/{trig_id}/subscriptions", json={"config": config, "reply_target": "dm_sender"})
    assert refused.status_code == 422, refused.text
    assert "reply_target" in refused.text and "direct messages" in refused.text
    listed = await client.get(f"/v1/triggers/{trig_id}/subscriptions")
    assert listed.json()["total"] == 0

    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-2", "provider": "slack", "config": {"app_token": "xapp-2-test", "bot_token": "xoxb-2-test"}},
    )
    seeded = await client.post(
        "/v1/channels", json={"id": "ch-2", "provider_id": "cp-2", "provider": "slack", "external_id": "C0002"},
    )
    assert seeded.status_code == 201, seeded.text

    explicit = {"channel_id": "ch-2", "anchor": "1700000000.000100", "quiet": False}
    created = await client.post(f"/v1/triggers/{trig_id}/subscriptions", json={"config": config, "reply_target": explicit})
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]
    assert created.json()["reply_target"] == explicit

    refused = await client.put(f"/v1/triggers/{trig_id}/subscriptions/{sub_id}", json={"reply_target": "dm_sender"})
    assert refused.status_code == 422, refused.text
    assert "reply_target" in refused.text
    updated = await client.put(f"/v1/triggers/{trig_id}/subscriptions/{sub_id}", json={"reply_target": "none"})
    assert updated.status_code == 200, updated.text
    got = await client.get(f"/v1/triggers/{trig_id}/subscriptions/{sub_id}")
    assert got.json()["reply_target"] == "none"


@pytest.mark.asyncio
async def test_an_explicit_reply_target_names_a_real_channel(client):
    """Round 2 of #932, item 3: an explicit ``{channel_id, anchor}`` target is validated at save time. An unknown channel is refused
    with a not-found (422) the same way ``set_reply_binding`` refuses one, and a Discord anchor must be a numeric thread id. Nothing
    is stored on a refusal, on create or update."""
    r = await client.post("/v1/triggers", json={"slug": "ch-rt", "name": "Channel", "config": {"kind": "channel", "provider_id": "cp-1"}})
    assert r.status_code == 201, r.text
    trig_id = r.json()["id"]
    config = {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "ag-1"}

    await client.post(
        "/v1/channel_providers",
        json={"id": "cp-dc", "provider": "discord", "config": {"bot_token": "x" * 60}},
    )
    seeded = await client.post(
        "/v1/channels", json={"id": "ch-dc", "provider_id": "cp-dc", "provider": "discord", "external_id": "500"},
    )
    assert seeded.status_code == 201, seeded.text

    refused = await client.post(
        f"/v1/triggers/{trig_id}/subscriptions",
        json={"config": config, "reply_target": {"channel_id": "ch-missing", "anchor": "1700000000.000100"}},
    )
    assert refused.status_code == 422, refused.text
    assert "reply_target" in refused.text and "ch-missing" in refused.text
    listed = await client.get(f"/v1/triggers/{trig_id}/subscriptions")
    assert listed.json()["total"] == 0

    refused = await client.post(
        f"/v1/triggers/{trig_id}/subscriptions",
        json={"config": config, "reply_target": {"channel_id": "ch-dc", "anchor": "not-a-snowflake"}},
    )
    assert refused.status_code == 422, refused.text
    assert "reply_target" in refused.text and "numeric" in refused.text

    created = await client.post(
        f"/v1/triggers/{trig_id}/subscriptions",
        json={"config": config, "reply_target": {"channel_id": "ch-dc", "anchor": "1234567890123"}},
    )
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]

    refused = await client.put(
        f"/v1/triggers/{trig_id}/subscriptions/{sub_id}",
        json={"reply_target": {"channel_id": "ch-missing"}},
    )
    assert refused.status_code == 422, refused.text
    assert "reply_target" in refused.text and "ch-missing" in refused.text
    got = await client.get(f"/v1/triggers/{trig_id}/subscriptions/{sub_id}")
    assert got.json()["reply_target"]["channel_id"] == "ch-dc"
