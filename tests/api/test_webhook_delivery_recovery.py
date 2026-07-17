"""Unit tests for ``recover_webhook_deliveries`` (D-C1 durability).

The webhook endpoint persists a pending ``WebhookDelivery`` row before its
fire-and-forget dispatch. Startup recovery re-dispatches stale ``pending``
rows the previous process dropped, and leaves ``done`` / fresh ``pending``
rows alone.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from primer.model.webhook_delivery import WebhookDelivery


async def _cancel(task: asyncio.Task) -> None:
    """Cancel a re-check task the way the lifespan's teardown does."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_recover_webhook_redispatches_stale_pending(fake_storage_provider):
    """A stale pending row is re-dispatched via _dispatch_webhook; a done
    row and a fresh pending row are not."""
    from primer.api._app_lifespan_phases import recover_webhook_deliveries

    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=1)
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
        recheck = await recover_webhook_deliveries(
            fake_storage_provider, None, None, None, None
        )

    assert dispatch_spy.await_count == 1
    args, kwargs = dispatch_spy.await_args
    assert args[0] == "trig-1"                 # trigger_id
    assert args[1] == {"webhook_body": "hi"}   # extra_context
    assert kwargs["delivery_id"] == "fire-stale"

    # 'fire-fresh' is inside the grace window, so the sweep hands back a
    # re-check task for it rather than abandoning it to the next boot.
    assert recheck is not None
    await _cancel(recheck)


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

    stale = datetime.now(timezone.utc) - timedelta(hours=1)
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
async def test_grace_skipped_row_is_dispatched_by_the_recheck(
    fake_storage_provider,
):
    """A row inside the grace window at sweep time IS eventually dispatched.

    The boot sweep is one-shot: without the re-check a row younger than the
    cutoff is skipped and never revisited by this process, so a delivery
    dropped inside the grace band waits for the NEXT boot - weeks away, or
    never, on a stable deployment. That is the silent-loss window this batch
    exists to close, so the grace must be a bound, not a deferral.
    """
    from primer.api import _app_lifespan_phases as phases

    storage = fake_storage_provider.get_storage(WebhookDelivery)
    await storage.create(WebhookDelivery(
        id="fire-young", trigger_id="trig-young", extra_context={},
        status="pending", created_at=datetime.now(timezone.utc),
    ))

    dispatch_spy = AsyncMock()
    # A grace short enough to keep the test fast, but long enough that the
    # row is reliably still inside the window when the sweep reads it.
    with patch.object(phases, "_WEBHOOK_DELIVERY_GRACE_SECS", 0.3), \
            patch("primer.api.routers.webhooks._dispatch_webhook", dispatch_spy):
        recheck = await phases.recover_webhook_deliveries(
            fake_storage_provider, None, None, None, None
        )
        # The one-shot pass skipped it: nothing dispatched yet.
        assert dispatch_spy.await_count == 0
        assert recheck is not None
        # The re-check sleeps until the row clears the window, then fires.
        await asyncio.wait_for(recheck, timeout=5)

    assert dispatch_spy.await_count == 1
    assert dispatch_spy.await_args.kwargs["delivery_id"] == "fire-young"
    # It self-terminates after the single pass - it is not a polling loop.
    assert recheck.done() and not recheck.cancelled()


@pytest.mark.asyncio
async def test_grace_recheck_task_cancels_cleanly_on_shutdown(
    fake_storage_provider,
):
    """The re-check task is cancellable and dispatches nothing once cancelled.

    It is owned by the lifespan and torn down like chat_tick_task /
    _claim_depth_task, so shutdown during the grace sleep must not leak the
    task or fire a delivery on the way out.
    """
    from primer.api._app_lifespan_phases import recover_webhook_deliveries

    storage = fake_storage_provider.get_storage(WebhookDelivery)
    await storage.create(WebhookDelivery(
        id="fire-young", trigger_id="trig-young", extra_context={},
        status="pending", created_at=datetime.now(timezone.utc),
    ))

    dispatch_spy = AsyncMock()
    with patch("primer.api.routers.webhooks._dispatch_webhook", dispatch_spy):
        # Default 300s grace: the task is parked in its sleep when we cancel.
        recheck = await recover_webhook_deliveries(
            fake_storage_provider, None, None, None, None
        )
        assert recheck is not None
        await _cancel(recheck)

    assert recheck.cancelled(), "re-check task did not cancel"
    assert dispatch_spy.await_count == 0
    # The row stays pending, so the next boot's sweep still owns it.
    assert (await storage.get("fire-young")).status == "pending"


@pytest.mark.asyncio
async def test_recover_webhook_skips_a_row_finalized_after_collection(
    fake_storage_provider,
):
    """A row finalized between collection and dispatch is NOT re-fired.

    The sweep collects ids and re-reads each one immediately before
    dispatching it, so a live sibling that finalizes a row mid-sweep is
    observed rather than raced: the re-read row is no longer 'pending' and
    the sweep skips it instead of duplicating the delivery.
    """
    from primer.api._app_lifespan_phases import recover_webhook_deliveries

    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    storage = fake_storage_provider.get_storage(WebhookDelivery)
    for _id in ("fire-a", "fire-sibling", "fire-b"):
        await storage.create(WebhookDelivery(
            id=_id, trigger_id=f"trig-{_id}", extra_context={},
            status="pending", created_at=stale,
        ))

    dispatched: list[str] = []

    async def _spy(*args, delivery_id=None, **kwargs):
        dispatched.append(delivery_id)
        # While dispatching the first row, a "live sibling" finalizes another
        # row that this sweep already collected the id of.
        if delivery_id == "fire-a":
            row = await storage.get("fire-sibling")
            await storage.update(row.model_copy(update={"status": "done"}))
        row = await storage.get(delivery_id)
        await storage.update(row.model_copy(update={"status": "done"}))

    with patch("primer.api.routers.webhooks._dispatch_webhook", _spy):
        await recover_webhook_deliveries(
            fake_storage_provider, None, None, None, None
        )

    assert "fire-sibling" not in dispatched, (
        "a row finalized after collection was re-fired"
    )
    assert sorted(dispatched) == ["fire-a", "fire-b"]
    # The sibling keeps the status its finaliser set, and the sweep never
    # charged it an attempt.
    sibling = await storage.get("fire-sibling")
    assert sibling.status == "done"
    assert sibling.attempts == 0


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

    stale = datetime.now(timezone.utc) - timedelta(hours=1)
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


def test_a_bad_grace_override_does_not_break_the_import():
    """A non-numeric override falls back to the default instead of raising.

    The value is parsed at import time, so letting ValueError escape means a
    typo'd env var takes the entire API down at boot.
    """
    import importlib

    from primer.api import _app_lifespan_phases as phases

    try:
        with patch.dict(
            "os.environ", {"PRIMER_WEBHOOK_RECOVERY_GRACE_SECS": "not-a-number"}
        ):
            reloaded = importlib.reload(phases)
            assert reloaded._WEBHOOK_DELIVERY_GRACE_SECS == (
                reloaded._WEBHOOK_DELIVERY_GRACE_SECS_DEFAULT
            )
    finally:
        importlib.reload(phases)


def test_grace_window_is_configurable_and_clears_a_slow_dispatch():
    """The grace window is the only guard against re-firing a live sibling's
    in-flight dispatch, so its default must clear a slow fresh-session
    dispatch (workspace + session creation), and operators must be able to
    raise it without a code change.
    """
    import importlib

    from primer.api import _app_lifespan_phases as phases

    assert phases._WEBHOOK_DELIVERY_GRACE_SECS >= 300

    with patch.dict(
        "os.environ", {"PRIMER_WEBHOOK_RECOVERY_GRACE_SECS": "900"}
    ):
        reloaded = importlib.reload(phases)
        assert reloaded._WEBHOOK_DELIVERY_GRACE_SECS == 900.0
    # Restore the module-level default for any later import of it.
    importlib.reload(phases)


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
