"""A failed subscription delivery must leave a findable record.

fire_trigger isolates every per-subscription failure and RETURNS it
(ok=False) rather than raising, so before this the only trace was
``Trigger.last_fire_error`` - the first failure of the LAST fire, cleared
by the next clean one. A catchup replay fires the same trigger up to 64
times then once more for the current tick, so N failed deliveries followed
by one healthy tick left nothing at all. ``Subscription.last_fired_at`` /
``last_fire_error`` (rendered by triggers.jsx) were never written anywhere.

Two destinations answer two different questions, and these tests keep them
apart:

* the Subscription row is LATEST state ("what happened last time");
* ``trigger.delivery_failed`` events are the HISTORY ("did the 03:00
  delivery ever arrive").
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import primer.trigger.dispatch as dispatch_mod
from primer.model.trigger import (
    ScheduledTriggerConfig,
    SessionAppendSubConfig,
    Subscription,
    Trigger,
)
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps, SubscriptionDispatchResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _ScriptedDispatcher:
    """Fails for subscription ids in ``failing``, succeeds otherwise."""

    def __init__(self, failing: set[str], message: str = "boom") -> None:
        self.failing = failing
        self.message = message

    async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
        if sub.id in self.failing:
            return SubscriptionDispatchResult(
                ok=False, error_code="dispatch_failed",
                error_message=self.message,
            )
        return SubscriptionDispatchResult(ok=True, artefact_id="art-1")


async def _seed(sp, *sub_ids: str):
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-1", slug="tr-1", name="t", description=None,
        config=ScheduledTriggerConfig(cron="0 * * * *", timezone="UTC"),
        enabled=True, next_fire_at=_now(), created_at=_now(),
    ))
    for sid in sub_ids:
        await sp.get_storage(Subscription).create(Subscription(
            id=sid, trigger_id="tr-1",
            config=SessionAppendSubConfig(session_id="s-1"),
            enabled=True, created_at=_now(),
        ))


def _deps(sp, fake_claim_engine, fake_scheduler) -> DispatchDeps:
    return DispatchDeps(
        storage_provider=sp, claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )


async def _failed_events(sp) -> list:
    events = await sp.get_event_store().read_after(0)
    return [e for e in events if e.event_type == "trigger.delivery_failed"]


@pytest.mark.asyncio
async def test_replayed_failures_survive_a_later_clean_fire(
    fake_storage_provider, fake_claim_engine, fake_scheduler, monkeypatch,
):
    """The catchup shape: several failing ticks, then one healthy one.

    Red before the fix: the trigger row ends clean (last_fire_error None)
    and nothing else records the three failures.
    """
    sp = fake_storage_provider
    await _seed(sp, "sb-bad", "sb-good")
    dispatcher = _ScriptedDispatcher({"sb-bad"})
    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: dispatcher)
    deps = _deps(sp, fake_claim_engine, fake_scheduler)

    base = datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)
    ticks = [base + timedelta(hours=i) for i in range(3)]
    for tick in ticks:
        await fire_trigger(trigger_id="tr-1", scheduled_for=tick, deps=deps)

    # The healthy sibling stops failing; the bad one is fixed too and the
    # current tick (scheduled_for=None) delivers cleanly to both.
    dispatcher.failing.clear()
    await fire_trigger(trigger_id="tr-1", scheduled_for=None, deps=deps)

    # The original loss: the trigger-level field only reflects the last fire.
    trigger = await sp.get_storage(Trigger).get("tr-1")
    assert trigger.last_fire_error is None

    # HISTORY: every failed delivery is findable, tied to its own tick.
    events = await _failed_events(sp)
    assert len(events) == 3
    assert {e.payload["scheduled_for"] for e in events} == {
        t.isoformat() for t in ticks
    }
    assert all(e.payload["subscription_id"] == "sb-bad" for e in events)
    assert all(e.payload["error_code"] == "dispatch_failed" for e in events)
    assert len({e.payload["fire_id"] for e in events}) == 3
    assert all(e.entity_id == "tr-1" for e in events)


@pytest.mark.asyncio
async def test_subscription_row_holds_latest_state_not_history(
    fake_storage_provider, fake_claim_engine, fake_scheduler, monkeypatch,
):
    sp = fake_storage_provider
    await _seed(sp, "sb-bad", "sb-good")
    dispatcher = _ScriptedDispatcher({"sb-bad"})
    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: dispatcher)
    deps = _deps(sp, fake_claim_engine, fake_scheduler)
    subs = sp.get_storage(Subscription)

    tick = datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)
    await fire_trigger(trigger_id="tr-1", scheduled_for=tick, deps=deps)

    bad = await subs.get("sb-bad")
    good = await subs.get("sb-good")
    # The slot triggers.jsx renders is now populated, per subscription,
    # and attributed: the healthy sibling is not tarred with the failure.
    assert bad.last_fired_at is not None
    blob = json.loads(bad.last_fire_error)
    assert blob["code"] == "dispatch_failed"
    assert blob["message"] == "boom"
    assert blob["scheduled_for"] == tick.isoformat()
    assert good.last_fired_at is not None
    assert good.last_fire_error is None

    # A later clean delivery clears it: this field is LATEST state, and
    # the delivery_failed events (not this field) are the history.
    dispatcher.failing.clear()
    await fire_trigger(trigger_id="tr-1", scheduled_for=None, deps=deps)
    assert (await subs.get("sb-bad")).last_fire_error is None
    assert len(await _failed_events(sp)) == 1


@pytest.mark.asyncio
async def test_skipped_deliveries_are_not_recorded_as_attempts(
    fake_storage_provider, fake_claim_engine, fake_scheduler, monkeypatch,
):
    """A skip (session busy/missing, no event match) never ran, so it is
    neither a failure event nor a "last fired" stamp."""
    sp = fake_storage_provider
    await _seed(sp, "sb-skip")

    class _Skipper:
        async def dispatch(self, sub, **_kw):
            return SubscriptionDispatchResult(
                ok=True, skipped=True, error_code="skipped_session_busy",
                error_message="in flight",
            )

    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: _Skipper())
    await fire_trigger(
        trigger_id="tr-1", scheduled_for=None,
        deps=_deps(sp, fake_claim_engine, fake_scheduler),
    )
    sub = await sp.get_storage(Subscription).get("sb-skip")
    assert sub.last_fired_at is None
    assert sub.last_fire_error is None
    assert await _failed_events(sp) == []


@pytest.mark.asyncio
async def test_event_and_row_never_carry_a_url_or_credential(
    fake_storage_provider, fake_claim_engine, fake_scheduler, monkeypatch,
):
    """The message is the str() of a caught exception; those embed URLs
    (webhook, DSN, SDK endpoint) with credentials in userinfo or query,
    and event redaction is key-name based so it never looks at values."""
    sp = fake_storage_provider
    await _seed(sp, "sb-bad")
    leaky = (
        "POST https://user:hunter2@hooks.example.com/x?token=abc123 failed; "
        "db postgresql://app:s3cr3t@db:5432/primer; Authorization: Bearer sk-live-999"
    )
    dispatcher = _ScriptedDispatcher({"sb-bad"}, message=leaky)
    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: dispatcher)
    await fire_trigger(
        trigger_id="tr-1", scheduled_for=None,
        deps=_deps(sp, fake_claim_engine, fake_scheduler),
    )

    sub = await sp.get_storage(Subscription).get("sb-bad")
    (event,) = await _failed_events(sp)
    for text in (sub.last_fire_error, json.dumps(event.payload)):
        for secret in ("hunter2", "abc123", "s3cr3t", "sk-live-999"):
            assert secret not in text, f"{secret!r} leaked into {text!r}"
    assert "<url>" in event.payload["error_message"]


@pytest.mark.asyncio
async def test_error_message_is_bounded(
    fake_storage_provider, fake_claim_engine, fake_scheduler, monkeypatch,
):
    sp = fake_storage_provider
    await _seed(sp, "sb-bad")
    monkeypatch.setattr(
        dispatch_mod, "get_dispatcher",
        lambda kind: _ScriptedDispatcher({"sb-bad"}, message="x" * 5000),
    )
    await fire_trigger(
        trigger_id="tr-1", scheduled_for=None,
        deps=_deps(sp, fake_claim_engine, fake_scheduler),
    )
    (event,) = await _failed_events(sp)
    assert len(event.payload["error_message"]) <= 310


@pytest.mark.asyncio
async def test_outcome_bookkeeping_failure_does_not_fail_the_fire(
    fake_storage_provider, fake_claim_engine, fake_scheduler, monkeypatch,
):
    """Recording is bookkeeping about a fire that already happened."""
    sp = fake_storage_provider
    await _seed(sp, "sb-bad")
    monkeypatch.setattr(
        dispatch_mod, "get_dispatcher",
        lambda kind: _ScriptedDispatcher({"sb-bad"}),
    )
    subs = sp.get_storage(Subscription)

    async def _boom(entity, *, conn=None):
        raise RuntimeError("storage down")

    monkeypatch.setattr(subs, "update", _boom)
    result = await fire_trigger(
        trigger_id="tr-1", scheduled_for=None,
        deps=_deps(sp, fake_claim_engine, fake_scheduler),
    )
    assert result.skipped is False
    assert result.results[0]["ok"] is False
    # The event (the history) still landed even though the row write failed.
    assert len(await _failed_events(sp)) == 1
