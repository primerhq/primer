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
async def test_recover_webhook_redispatches_every_row_across_pages(
    fake_storage_provider,
):
    """EVERY stale pending row is dispatched exactly once, even when the
    stale set spans more than one page.

    Regression: the sweep used to dispatch inside the paging loop, and each
    dispatch flips its row out of the 'pending' set being paged - mutating
    the predicate mid-page. With 250 rows and a 200-row page, offset=0 drained
    rows 0-199, which left only 50 pending; the next query at offset=200 then
    landed past the end and returned nothing, silently skipping rows 200-249.
    """
    from primer.api._app_lifespan_phases import recover_webhook_deliveries

    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    storage = fake_storage_provider.get_storage(WebhookDelivery)
    total = 250  # deliberately > the 200-row recovery page size
    for i in range(total):
        await storage.create(WebhookDelivery(
            id=f"fire-{i:04d}", trigger_id=f"trig-{i}",
            extra_context={}, status="pending", created_at=stale,
        ))

    dispatched: list[str] = []

    async def _spy(*args, delivery_id=None, **kwargs):
        # Mimic the real dispatcher: it finalizes the row, which removes it
        # from the 'pending' set the recovery query pages over.
        dispatched.append(delivery_id)
        row = await storage.get(delivery_id)
        await storage.update(row.model_copy(update={"status": "done"}))

    with patch("primer.api.routers.webhooks._dispatch_webhook", _spy):
        await recover_webhook_deliveries(
            fake_storage_provider, None, None, None, None
        )

    assert len(dispatched) == total, f"only {len(dispatched)}/{total} dispatched"
    assert sorted(dispatched) == sorted(f"fire-{i:04d}" for i in range(total))


@pytest.mark.asyncio
async def test_recover_webhook_gives_up_at_the_attempt_cap(fake_storage_provider):
    """A row at the attempt cap is marked failed and NOT re-fired.

    Without the cap a poison-pill row (its dispatch hard-crashes the process,
    or _finalize_delivery's swallowed update never marks it) stays pending and
    is re-fired on EVERY subsequent boot forever, each time spawning duplicate
    chats/sessions.
    """
    from primer.api._app_lifespan_phases import (
        _WEBHOOK_DELIVERY_MAX_ATTEMPTS,
        recover_webhook_deliveries,
    )

    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    storage = fake_storage_provider.get_storage(WebhookDelivery)
    await storage.create(WebhookDelivery(
        id="fire-poison", trigger_id="trig-poison", extra_context={},
        status="pending", created_at=stale,
        attempts=_WEBHOOK_DELIVERY_MAX_ATTEMPTS,
    ))
    await storage.create(WebhookDelivery(
        id="fire-under-cap", trigger_id="trig-ok", extra_context={},
        status="pending", created_at=stale,
        attempts=_WEBHOOK_DELIVERY_MAX_ATTEMPTS - 1,
    ))

    dispatch_spy = AsyncMock()
    with patch("primer.api.routers.webhooks._dispatch_webhook", dispatch_spy):
        await recover_webhook_deliveries(
            fake_storage_provider, None, None, None, None
        )

    # The exhausted row is abandoned, not re-fired.
    assert dispatch_spy.await_count == 1
    assert dispatch_spy.await_args.kwargs["delivery_id"] == "fire-under-cap"
    poisoned = await storage.get("fire-poison")
    assert poisoned.status == "failed"
    assert poisoned.completed_at is not None

    # The under-cap row is re-fired, and its attempt is recorded BEFORE the
    # dispatch so an attempt that kills the process still counts.
    assert (await storage.get("fire-under-cap")).attempts == (
        _WEBHOOK_DELIVERY_MAX_ATTEMPTS
    )


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
