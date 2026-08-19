"""S6 P2: interactive webhooks hold, cap, and stay backward compatible.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 4.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from primer.model.trigger import WebhookTriggerConfig
from primer.model.webhook_delivery import WebhookDelivery
from primer.trigger.dispatch import FireResult
from primer.trigger.hold import HeldFire
from primer.trigger.service import ServiceDeps, create_trigger


async def _trigger(sp, slug, **cfg):
    return await create_trigger(
        slug=slug, name="WH", description=None,
        config=WebhookTriggerConfig(**cfg), enabled=True,
        deps=ServiceDeps(storage_provider=sp),
    )


async def test_interactive_returns_the_run_result(client, fake_storage_provider):
    trigger = await _trigger(
        fake_storage_provider, "wh-hold-ok", interactive=True,
    )
    held = HeldFire(
        fire_result=FireResult(fire_id="fire-1", results=[]),
        results=[{"artefact_id": "s1", "final_text": "all done"}],
    )
    with patch(
        "primer.api.routers.webhooks.fire_and_hold",
        AsyncMock(return_value=held),
    ):
        r = await client.post(
            f"/v1/webhooks/{trigger.config.token}", content=b"{}",
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fire_id"] == "fire-1"
    assert body["results"] == [{"artefact_id": "s1", "final_text": "all done"}]


async def test_wait_cap_falls_back_to_202_pending(client, fake_storage_provider):
    trigger = await _trigger(
        fake_storage_provider, "wh-hold-cap", interactive=True,
        wait_timeout_seconds=1,
    )
    held = HeldFire(
        fire_result=FireResult(fire_id="fire-2", results=[]),
        results=[],
        timed_out=True,
    )
    with patch(
        "primer.api.routers.webhooks.fire_and_hold",
        AsyncMock(return_value=held),
    ):
        r = await client.post(
            f"/v1/webhooks/{trigger.config.token}", content=b"{}",
        )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["delivery_id"].startswith("fire-")


async def test_non_interactive_is_unchanged(client, fake_storage_provider):
    """Regression: the default webhook still returns 202 accepted."""
    trigger = await _trigger(fake_storage_provider, "wh-hold-off")
    with patch(
        "primer.api.routers.webhooks.fire_and_hold",
        AsyncMock(side_effect=AssertionError("must not hold")),
    ):
        r = await client.post(
            f"/v1/webhooks/{trigger.config.token}", content=b"{}",
        )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "accepted"


async def test_completed_hold_records_results_on_the_delivery(
    client, fake_storage_provider,
):
    trigger = await _trigger(
        fake_storage_provider, "wh-hold-row", interactive=True,
    )
    held = HeldFire(
        fire_result=FireResult(fire_id="fire-4", results=[]),
        results=[{"artefact_id": "s4", "final_text": "ok"}],
    )
    with patch(
        "primer.api.routers.webhooks.fire_and_hold",
        AsyncMock(return_value=held),
    ):
        r = await client.post(
            f"/v1/webhooks/{trigger.config.token}", content=b"{}",
        )
    assert r.status_code == 200
    rows = fake_storage_provider.get_storage(WebhookDelivery)
    row = await rows.get(r.json()["delivery_id"]) if "delivery_id" in r.json() else None
    if row is None:
        # The 200 body carries no delivery_id; find the single row instead.
        from primer.model.storage import OffsetPage
        page = await rows.find(None, OffsetPage(offset=0, length=10))
        row = page.items[0]
    assert row.status == "done"
    assert row.fire_id == "fire-4"
    assert row.results == [{"artefact_id": "s4", "final_text": "ok"}]
