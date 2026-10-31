"""A FULL grantee of a trigger cannot drive the owner's out-of-reach subscriptions through REST (final review F1, anti-goals 1 and 2).

The tool door refuses these calls (``reach=caller_scope(ctx)``); the REST routes must apply the same subscription-target reach
check, and a webhook token never reaches a caller below OWNER.
"""
from __future__ import annotations

from datetime import datetime, timezone

from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.trigger import AgentFreshSubConfig, Subscription, Trigger, WebhookTriggerConfig
from primer.trigger.owner import WEBHOOK_TOKEN_MASK
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.conftest import fake_storage_provider  # noqa: F401
from tests.trigger.test_channel_trigger_access import _login, _register_admin, _seed_user, _seed_workspace

NOW = datetime.now(timezone.utc)
TOKEN = "a" * 32


async def _world(client, app):  # noqa: F811
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    carol = await _seed_user(app, "carol")
    sp = app.state.storage_provider
    await _seed_workspace(app, "ws-alice", alice)  # alice-only: carol holds nothing there
    aref = PrincipalRef(type="user", id=alice, display="alice", role="user", source="local", user_id=alice)
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-wh", slug="wh", name="w", created_at=NOW, owner=aref, config=WebhookTriggerConfig(token=TOKEN),
        access=Access(owners=[alice], grants={carol: "full"}),
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-a", trigger_id="tr-wh", created_at=NOW, owner=aref,
        config=AgentFreshSubConfig(workspace_id="ws-alice", agent_id="ag-1"), payload_template="{{ webhook_body }}",
    ))
    return sp, alice, carol


async def test_rename_by_a_grantee_with_an_out_of_reach_subscription_is_refused(client, app):  # noqa: F811
    sp, alice, _ = await _world(client, app)
    await _login(client, "carol")
    put = await client.put("/v1/triggers/tr-wh", json={"name": "renamed"})
    assert put.status_code == 404, put.text
    assert TOKEN not in put.text
    stored = await sp.get_storage(Trigger).get("tr-wh")
    assert stored.name == "w" and stored.config.token == TOKEN and stored.owner.id == alice


async def test_fire_now_and_delete_by_a_grantee_are_refused(client, app):  # noqa: F811
    sp, _, _ = await _world(client, app)
    await _login(client, "carol")
    assert (await client.post("/v1/triggers/tr-wh/fire_now")).status_code == 404
    assert (await client.delete("/v1/triggers/tr-wh")).status_code in (403, 404)
    assert await sp.get_storage(Trigger).get("tr-wh") is not None


async def test_subscription_routes_hide_and_protect_an_out_of_reach_subscription(client, app):  # noqa: F811
    sp, _, _ = await _world(client, app)
    await _login(client, "carol")
    listed = await client.get("/v1/triggers/tr-wh/subscriptions")
    assert listed.status_code == 200 and listed.json()["items"] == []
    assert (await client.get("/v1/triggers/tr-wh/subscriptions/sb-a")).status_code == 404
    assert (await client.put("/v1/triggers/tr-wh/subscriptions/sb-a", json={"enabled": False})).status_code == 404
    assert (await client.delete("/v1/triggers/tr-wh/subscriptions/sb-a")).status_code == 404
    assert await sp.get_storage(Subscription).get("sb-a") is not None


async def test_a_grantee_below_owner_never_reads_the_webhook_token(client, app):  # noqa: F811
    sp, _, carol = await _world(client, app)
    await sp.get_storage(Subscription).delete("sb-a")  # nothing out of reach: carol may now save
    await _login(client, "carol")
    put = await client.put("/v1/triggers/tr-wh", json={"name": "renamed"})
    assert put.status_code == 200, put.text
    assert put.json()["config"]["token"] == WEBHOOK_TOKEN_MASK
    got = await client.get("/v1/triggers/tr-wh")
    assert got.json()["config"]["token"] == WEBHOOK_TOKEN_MASK
    # a rename by a full grantee does not cut the owner's webhook off: the stored token is unchanged
    assert (await sp.get_storage(Trigger).get("tr-wh")).config.token == TOKEN
