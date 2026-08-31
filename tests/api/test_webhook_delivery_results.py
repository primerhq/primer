"""S6 P2: deliveries carry the fire result so the poll endpoint can serve it.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 4.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.api.routers.webhooks import _finalize_delivery
from primer.model.webhook_delivery import WebhookDelivery


def test_result_fields_default_empty():
    row = WebhookDelivery(
        id="fire-x", trigger_id="tr-1", created_at=datetime.now(UTC),
    )
    assert row.fire_id is None
    assert row.results == []


async def test_finalize_writes_fire_id_and_results(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WebhookDelivery)
    await storage.create(WebhookDelivery(
        id="fire-y", trigger_id="tr-1", created_at=datetime.now(UTC),
    ))
    await _finalize_delivery(
        fake_storage_provider, "fire-y", ok=True,
        fire_id="fire-abc",
        results=[{"artefact_id": "s1", "final_text": "all done"}],
    )
    row = await storage.get("fire-y")
    assert row.status == "done"
    assert row.completed_at is not None
    assert row.fire_id == "fire-abc"
    assert row.results == [{"artefact_id": "s1", "final_text": "all done"}]


async def test_finalize_without_results_is_unchanged(fake_storage_provider):
    """The non-interactive path still just flips the status."""
    storage = fake_storage_provider.get_storage(WebhookDelivery)
    await storage.create(WebhookDelivery(
        id="fire-z", trigger_id="tr-1", created_at=datetime.now(UTC),
    ))
    await _finalize_delivery(fake_storage_provider, "fire-z", ok=False)
    row = await storage.get("fire-z")
    assert row.status == "failed"
    assert row.fire_id is None
    assert row.results == []
