"""S6 P2: polling a webhook delivery for its result.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 4 - same token
capability, same rate-limit bucket.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.model.trigger import WebhookTriggerConfig
from primer.model.webhook_delivery import WebhookDelivery
from primer.trigger.service import ServiceDeps, create_trigger


async def _trigger(sp, slug):
    return await create_trigger(
        slug=slug, name="WH", description=None,
        config=WebhookTriggerConfig(interactive=True), enabled=True,
        deps=ServiceDeps(storage_provider=sp),
    )


async def test_poll_returns_the_recorded_result(client, fake_storage_provider):
    trigger = await _trigger(fake_storage_provider, "wh-poll-ok")
    await fake_storage_provider.get_storage(WebhookDelivery).create(
        WebhookDelivery(
            id="fire-poll-1", trigger_id=trigger.id,
            created_at=datetime.now(UTC), status="done",
            fire_id="fire-abc",
            results=[{"artefact_id": "s1", "final_text": "done"}],
        )
    )
    r = await client.get(
        f"/v1/webhooks/{trigger.config.token}/deliveries/fire-poll-1"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    assert body["fire_id"] == "fire-abc"
    assert body["results"] == [{"artefact_id": "s1", "final_text": "done"}]


async def test_poll_pending_delivery(client, fake_storage_provider):
    trigger = await _trigger(fake_storage_provider, "wh-poll-pending")
    await fake_storage_provider.get_storage(WebhookDelivery).create(
        WebhookDelivery(
            id="fire-poll-2", trigger_id=trigger.id,
            created_at=datetime.now(UTC), status="pending",
        )
    )
    r = await client.get(
        f"/v1/webhooks/{trigger.config.token}/deliveries/fire-poll-2"
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    assert r.json()["results"] == []


async def test_poll_unknown_token_is_404(client, fake_storage_provider):
    r = await client.get("/v1/webhooks/" + "0" * 32 + "/deliveries/whatever")
    assert r.status_code == 404, r.text
    assert r.json()["extensions"]["code"] == "webhook_not_found"


async def test_poll_foreign_delivery_is_404(client, fake_storage_provider):
    trigger = await _trigger(fake_storage_provider, "wh-poll-foreign")
    await fake_storage_provider.get_storage(WebhookDelivery).create(
        WebhookDelivery(
            id="fire-poll-3", trigger_id="tr-someone-else",
            created_at=datetime.now(UTC),
        )
    )
    r = await client.get(
        f"/v1/webhooks/{trigger.config.token}/deliveries/fire-poll-3"
    )
    assert r.status_code == 404, r.text
    assert r.json()["extensions"]["code"] == "delivery_not_found"
