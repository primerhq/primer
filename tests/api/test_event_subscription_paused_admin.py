"""Pausing an event subscription is an admin act (final review F3, spec decision 3): a plain user cannot pause system-cdc or anyone's wait."""
from __future__ import annotations

from primer.model.event import EventSubscription, LogSink
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.conftest import fake_storage_provider  # noqa: F401
from tests.trigger.test_channel_trigger_access import _login, _register_admin, _seed_user


async def _sub(app) -> None:
    """The boot seeds ``system-cdc``; make sure it is there for a bare app."""
    storage = app.state.storage_provider.get_storage(EventSubscription)
    if await storage.get("system-cdc") is None:
        await storage.create(EventSubscription(id="system-cdc", sink=LogSink(), managed_by="system"))


async def test_a_plain_user_cannot_pause_a_subscription(client, app):  # noqa: F811
    await _register_admin(client)
    await _seed_user(app, "alice")
    await _sub(app)
    await _login(client, "alice")
    r = await client.post("/v1/event_subscriptions/system-cdc/paused", json={"paused": True})
    assert r.status_code in (403, 404), r.text
    assert (await app.state.storage_provider.get_storage(EventSubscription).get("system-cdc")).paused is False


async def test_an_admin_still_pauses_a_subscription(client, app):  # noqa: F811
    await _register_admin(client)
    await _sub(app)
    r = await client.post("/v1/event_subscriptions/system-cdc/paused", json={"paused": True})
    assert r.status_code == 200 and r.json()["paused"] is True
