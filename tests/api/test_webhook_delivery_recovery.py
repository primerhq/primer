"""Unit tests for ``recover_webhook_deliveries`` (D-C1 durability).

The webhook endpoint persists a pending ``WebhookDelivery`` row before its
fire-and-forget dispatch. Startup recovery re-dispatches stale ``pending``
rows the previous process dropped, and leaves ``done`` / fresh ``pending``
rows alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from primer.model.webhook_delivery import WebhookDelivery


@pytest.mark.asyncio
async def test_recover_webhook_redispatches_stale_pending(fake_storage_provider):
    """A stale pending row is re-dispatched via _dispatch_webhook; a done
    row and a fresh pending row are not."""
    from primer.api._app_lifespan_phases import recover_webhook_deliveries

    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=5)
    storage = fake_storage_provider.get_storage(WebhookDelivery)
    await storage.create(WebhookDelivery(
        id="fire-stale", trigger_id="trig-1",
        extra_context={"webhook_body": "hi"},
        status="pending", created_at=stale,
    ))
    await storage.create(WebhookDelivery(
        id="fire-done", trigger_id="trig-2",
        extra_context={}, status="done", created_at=stale,
        completed_at=stale,
    ))
    await storage.create(WebhookDelivery(
        id="fire-fresh", trigger_id="trig-3",
        extra_context={}, status="pending", created_at=now,
    ))

    dispatch_spy = AsyncMock()
    with patch(
        "primer.api.routers.webhooks._dispatch_webhook", dispatch_spy
    ):
        await recover_webhook_deliveries(
            fake_storage_provider, None, None, None, None
        )

    assert dispatch_spy.await_count == 1
    args, kwargs = dispatch_spy.await_args
    assert args[0] == "trig-1"                 # trigger_id
    assert args[1] == {"webhook_body": "hi"}   # extra_context
    assert kwargs["delivery_id"] == "fire-stale"


@pytest.mark.asyncio
async def test_recover_webhook_noop_when_no_pending(fake_storage_provider):
    """No pending rows → dispatcher never called."""
    from primer.api._app_lifespan_phases import recover_webhook_deliveries

    dispatch_spy = AsyncMock()
    with patch(
        "primer.api.routers.webhooks._dispatch_webhook", dispatch_spy
    ):
        await recover_webhook_deliveries(
            fake_storage_provider, None, None, None, None
        )
    assert dispatch_spy.await_count == 0
