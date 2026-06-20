"""REST round-trip for a channel trigger + a matched subscription.

Creates a ``channel`` trigger, then a subscription carrying both an
``event_matcher`` and a ``reply_target``, and asserts they round-trip
through a follow-up GET.
"""

from __future__ import annotations

import pytest


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
