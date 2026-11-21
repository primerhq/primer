"""An interactive webhook's delivery row records its fire as soon as the fire returns (W12).

The hold keeps the row pending for the poll endpoint until the run ends, which can be past the recovery grace. A
recovery sweep that runs meanwhile (a replica booting during a rolling deploy) must see the fire on the row and not
fire the trigger a second time. Characterization tests pin the rows, the poll answers and the recovery of a row that
never fired.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from primer.api import _app_lifespan_phases as phases
from primer.api.routers import webhooks
from primer.bus.in_memory import InMemoryEventBus
from primer.model.storage import OffsetPage
from primer.model.trigger import WebhookTriggerConfig
from primer.model.webhook_delivery import WebhookDelivery
from primer.trigger.dispatch import FireResult
from primer.trigger.service import ServiceDeps, create_trigger


async def _trigger(sp, slug, **cfg):
    return await create_trigger(
        slug=slug, name="WH", description=None,
        config=WebhookTriggerConfig(**cfg), enabled=True,
        deps=ServiceDeps(storage_provider=sp),
    )


async def _only_row(sp) -> WebhookDelivery:
    page = await sp.get_storage(WebhookDelivery).find(None, OffsetPage(offset=0, length=10))
    assert len(page.items) == 1
    return page.items[0]


async def _await_post_phase(event, post, *, timeout, phase):
    """Observe a held POST milestone without hiding an earlier request failure."""
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait((waiter, post), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            await waiter
            return
        if post in done:
            response = await post  # preserve an actual request exception
            raise AssertionError(f'POST ended before {phase}: HTTP {response.status_code}')
        raise TimeoutError(f'POST did not reach {phase} within {timeout}s')
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


# --------------------------------------------------------------------------- characterization


async def test_non_interactive_row_ends_done_with_one_attempt(client, fake_storage_provider):
    trigger = await _trigger(fake_storage_provider, "wh-c-plain")

    async def _fire(**_kw):
        return SimpleNamespace(fire_id="fire-plain", results=[])

    with patch("primer.api.routers.webhooks.fire_trigger", side_effect=_fire):
        r = await client.post(f"/v1/webhooks/{trigger.config.token}", content=b"{}")
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"
    row = await _only_row(fake_storage_provider)
    assert (row.id, row.status, row.attempts, row.results) == (r.json()["delivery_id"], "done", 1, [])
    poll = await client.get(f"/v1/webhooks/{trigger.config.token}/deliveries/{row.id}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "done" and poll.json()["results"] == []


async def test_interactive_row_inside_the_cap_records_fire_and_results(client, fake_storage_provider, monkeypatch, app):
    trigger = await _trigger(fake_storage_provider, "wh-c-held", interactive=True)
    bus = InMemoryEventBus()
    await bus.initialize()
    monkeypatch.setattr(app.state, "event_bus", bus)

    async def _fire(**_kw):
        await bus.publish("session:s1:terminal", {"status": "ended"})
        return FireResult(fire_id="fire-held", results=[{"ok": True, "artefact_id": "s1"}])

    monkeypatch.setattr("primer.trigger.hold.fire_trigger", _fire)
    try:
        r = await client.post(f"/v1/webhooks/{trigger.config.token}", content=b"{}")
    finally:
        await bus.aclose()
    assert r.status_code == 200, r.text
    assert r.json() == {"fire_id": "fire-held", "results": [{"artefact_id": "s1", "final_text": None}]}
    row = await _only_row(fake_storage_provider)
    assert (row.status, row.attempts, row.fire_id) == ("done", 1, "fire-held")
    assert row.results == [{"artefact_id": "s1", "final_text": None}]


async def test_recovery_refires_a_stale_pending_row_without_a_fire(fake_storage_provider, monkeypatch):
    from datetime import UTC, datetime, timedelta

    fires: list[str] = []

    async def _fire(**kw):
        fires.append(kw["trigger_id"])
        return FireResult(fire_id="fire-again", results=[])

    monkeypatch.setattr(webhooks, "fire_trigger", _fire)
    storage = fake_storage_provider.get_storage(WebhookDelivery)
    await storage.create(WebhookDelivery(
        id="d-stale", trigger_id="tr-1", status="pending", attempts=1,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    ))
    assert await phases.recover_webhook_deliveries(fake_storage_provider, None, None, None, None) is None
    assert fires == ["tr-1"]
    row = await storage.get("d-stale")
    assert (row.status, row.attempts) == ("done", 2)


# --------------------------------------------------------------------------- W12


async def test_W12_a_held_fire_outliving_the_grace_is_not_refired_by_recovery(
    client, fake_storage_provider, monkeypatch, app,
):
    """The run outlives the grace while the POST is still held: the sweep must see the fire on the row."""
    trigger = await _trigger(fake_storage_provider, "wh-w12-held", interactive=True, wait_timeout_seconds=5)
    bus = InMemoryEventBus()
    await bus.initialize()
    monkeypatch.setattr(app.state, "event_bus", bus)
    fires: list[str] = []
    fired = asyncio.Event()
    hold_entered = asyncio.Event()
    hold_tasks: list[asyncio.Task] = []
    real_fire_and_hold = webhooks.fire_and_hold

    async def _hold(**kw):
        # The endpoint owns a separate hold task, which request cancellation
        # alone need not stop before its caller-visible wait cap expires.
        task = asyncio.current_task()
        assert task is not None
        hold_tasks.append(task)
        hold_entered.set()
        return await real_fire_and_hold(**kw)

    monkeypatch.setattr(webhooks, "fire_and_hold", _hold)

    async def _fire(**kw):
        fires.append(kw["trigger_id"])
        fired.set()
        return FireResult(fire_id=f"fire-{len(fires)}", results=[{"ok": True, "artefact_id": "s1"}])

    monkeypatch.setattr("primer.trigger.hold.fire_trigger", _fire)
    monkeypatch.setattr(webhooks, "fire_trigger", _fire)
    post = asyncio.create_task(client.post(f"/v1/webhooks/{trigger.config.token}", content=b"{}"))
    try:
        # Request admission (middleware/token lookup/row creation) is setup,
        # not recovery behavior. Keep a separate bounded readiness deadline and
        # retain the five-second fire deadline after reaching the real hold.
        await _await_post_phase(hold_entered, post, timeout=20, phase='hold entry')
        await _await_post_phase(fired, post, timeout=5, phase='first fire')
        for _ in range(100):  # the run is held; give the fire's row write its moment
            row = await _only_row(fake_storage_provider)
            if row.fire_id:
                break
            await asyncio.sleep(0.01)
        assert row.fire_id == "fire-1", "fire must be durably recorded before recovery"
        # The fake clock: the row is past the grace, as on a replica booting 6 minutes into a 10-minute run.
        monkeypatch.setattr(phases, "_WEBHOOK_DELIVERY_GRACE_SECS", 0.0)
        await phases.recover_webhook_deliveries(fake_storage_provider, None, None, None, None)
        assert fires == [trigger.id]
        poll = await client.get(f"/v1/webhooks/{trigger.config.token}/deliveries/{row.id}")
        assert poll.json()["status"] == "pending"  # the poll still says the run is going
        assert poll.json()["fire_id"] == "fire-1"
        await bus.publish("session:s1:terminal", {"status": "ended"})
        r = await asyncio.wait_for(post, 10)
        assert r.status_code == 200
        row = await _only_row(fake_storage_provider)
        assert (row.status, row.fire_id, row.attempts) == ("done", "fire-1", 1)
    finally:
        # A failed startup/assertion must not publish before the hold subscribes
        # and replace the original failure with a second POST timeout.
        try:
            for task in (post, *hold_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(post, *hold_tasks, return_exceptions=True)
        finally:
            await bus.aclose()


async def test_W12_a_hold_without_a_bus_records_its_fire(fake_storage_provider):
    """The no-bus branch of the hold fires and reports a timeout; the callback still sees the fire."""
    from primer.trigger.hold import fire_and_hold
    from primer.trigger.subscribers import DispatchDeps

    seen: list[str] = []

    async def _on_fired(result):
        seen.append(result.fire_id)

    async def _fire(**_kw):
        return FireResult(fire_id="fire-nobus", results=[])

    with patch("primer.trigger.hold.fire_trigger", _fire):
        held = await fire_and_hold(
            trigger_id="tr-1", extra_context={}, deps=DispatchDeps(storage_provider=fake_storage_provider, claim_engine=None),
            workspace_registry=None, wait_timeout=1.0, on_fired=_on_fired,
        )
    assert held.timed_out is True
    assert seen == ["fire-nobus"]


async def test_W12_non_interactive_row_records_the_fire_id(client, fake_storage_provider):
    trigger = await _trigger(fake_storage_provider, "wh-w12-plain")

    async def _fire(**_kw):
        return SimpleNamespace(fire_id="fire-plain", results=[])

    with patch("primer.api.routers.webhooks.fire_trigger", side_effect=_fire):
        await client.post(f"/v1/webhooks/{trigger.config.token}", content=b"{}")
    row = await _only_row(fake_storage_provider)
    assert (row.status, row.fire_id) == ("done", "fire-plain")


async def test_W12_recovery_skips_a_pending_row_that_carries_a_fire(fake_storage_provider, monkeypatch):
    from datetime import UTC, datetime, timedelta

    fires: list[str] = []

    async def _fire(**kw):
        fires.append(kw["trigger_id"])
        return FireResult(fire_id="fire-again", results=[])

    monkeypatch.setattr(webhooks, "fire_trigger", _fire)
    storage = fake_storage_provider.get_storage(WebhookDelivery)
    await storage.create(WebhookDelivery(
        id="d-fired", trigger_id="tr-1", status="pending", attempts=1, fire_id="fire-1",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    ))
    await phases.recover_webhook_deliveries(fake_storage_provider, None, None, None, None)
    assert fires == []
    row = await storage.get("d-fired")
    assert (row.status, row.attempts) == ("pending", 1)


@pytest.mark.parametrize('outcome', ['response', 'exception', 'not-ready'])
async def test_held_post_phase_reports_request_failure_and_drains_waiter(outcome):
    event = asyncio.Event()
    release = asyncio.Event()

    async def request():
        if outcome == 'response':
            return SimpleNamespace(status_code=403)
        if outcome == 'exception':
            raise ValueError('request failed before hold')
        await release.wait()

    post = asyncio.create_task(request())
    before = set(asyncio.all_tasks())
    try:
        if outcome == 'response':
            with pytest.raises(AssertionError, match='before hold entry: HTTP 403'):
                await _await_post_phase(event, post, timeout=1, phase='hold entry')
        elif outcome == 'exception':
            with pytest.raises(ValueError, match='request failed before hold'):
                await _await_post_phase(event, post, timeout=1, phase='hold entry')
        else:
            # Neither coroutine can satisfy readiness, regardless of scheduling.
            with pytest.raises(TimeoutError, match='did not reach hold entry'):
                await _await_post_phase(event, post, timeout=0, phase='hold entry')
            assert not post.done()  # outer test retains request-cleanup ownership
        assert not (set(asyncio.all_tasks()) - before)
    finally:
        if not post.done():
            post.cancel()
        await asyncio.gather(post, return_exceptions=True)


async def test_held_post_phase_readiness_does_not_require_post_completion():
    entered, fire, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def request():
        entered.set()
        await release.wait()
        fire.set()
        await asyncio.Event().wait()

    post = asyncio.create_task(request())
    try:
        await _await_post_phase(entered, post, timeout=1, phase='hold entry')
        assert not fire.is_set() and not post.done()
        release.set()
        await _await_post_phase(fire, post, timeout=1, phase='first fire')
        assert not post.done()  # a held response must stay pending during recovery
    finally:
        post.cancel()
        await asyncio.gather(post, return_exceptions=True)
