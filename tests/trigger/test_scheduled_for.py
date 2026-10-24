"""Every clock fire carries the tick it is for as ``scheduled_for``, on time or replayed (ticket 01a16ddb, ruling 4).

The due tick of a ``scheduled`` trigger, each catch-up's own tick, and a ``delayed`` trigger's ``fire_at``. It used to be null on every on-time
fire (the worker passed ``None`` for the current tick), so a ``{{ scheduled_for }}`` template rendered ``None``; only a ``catchup: all`` replay set
it. Driven through the worker's trigger handler with the real ``fire_trigger`` and a recording dispatcher, on a fake clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import primer.trigger.dispatch as dispatch_mod
from primer.model.trigger import DelayedTriggerConfig, ScheduledTriggerConfig, SessionAppendSubConfig, Subscription, Trigger
from primer.trigger.subscribers import SubscriptionDispatchResult
from primer.worker.engine_handlers import run_engine_trigger


def tick(day: int) -> datetime:
    """The daily 09:00 UTC tick of 2026-01-``day``."""
    return datetime(2026, 1, day, 9, 0, tzinfo=timezone.utc)


#: The fake clock: five seconds after the 09:00 tick of 2026-01-15.
NOW = tick(15) + timedelta(seconds=5)


def _scheduled(catchup: str, *, next_fire_at: datetime, last_fired_at: datetime | None) -> Trigger:
    return Trigger(
        id="tr-cron", slug="daily-digest", name="d", config=ScheduledTriggerConfig(cron="0 9 * * *", timezone="UTC", catchup=catchup),
        enabled=True, next_fire_at=next_fire_at, last_fired_at=last_fired_at, created_at=tick(1),
    )


async def _fire_for_real(sp, monkeypatch, *, now: datetime) -> list[str]:
    """Run the handler with the real ``fire_trigger``: the payload each delivery rendered (``{{ scheduled_for }}``)."""
    rendered: list[str] = []

    class _Records:
        async def dispatch(self, sub, *, rendered_payload, **_kw):
            rendered.append(rendered_payload)
            return SubscriptionDispatchResult(ok=True, artefact_id="sess-1")

    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: _Records())
    monkeypatch.setattr("primer.worker.engine_handlers._now", lambda: now, raising=False)
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-1", trigger_id="tr-cron", config=SessionAppendSubConfig(session_id="sess-1"), payload_template="{{ scheduled_for }}",
        created_at=tick(1),
    ))

    async def _release(lease, outcome) -> None:
        return None

    pool = SimpleNamespace(_storage=sp, _engine=None, _scheduler=None, _workspace_registry=None, _event_bus=None, _release_lease=_release)
    await run_engine_trigger(pool, SimpleNamespace(entity_id="tr-cron"))  # type: ignore[arg-type]
    return rendered


async def test_an_on_time_scheduled_fire_carries_its_tick(fake_storage_provider, monkeypatch):
    await fake_storage_provider.get_storage(Trigger).create(
        _scheduled("one", next_fire_at=tick(15), last_fired_at=tick(14) + timedelta(seconds=2)),
    )

    rendered = await _fire_for_real(fake_storage_provider, monkeypatch, now=NOW)

    assert rendered == [tick(15).isoformat()]
    [fired] = [e for e in await fake_storage_provider.get_event_store().read_after(0) if e.event_type == "trigger.fired"]
    assert fired.payload["scheduled_for"] == tick(15).isoformat()


async def test_a_catch_up_and_the_due_tick_each_carry_their_own_tick(fake_storage_provider, monkeypatch):
    await fake_storage_provider.get_storage(Trigger).create(
        _scheduled("all", next_fire_at=tick(13), last_fired_at=tick(12) + timedelta(seconds=2)),
    )

    rendered = await _fire_for_real(fake_storage_provider, monkeypatch, now=NOW)

    assert rendered == [tick(13).isoformat(), tick(14).isoformat(), tick(15).isoformat()]


async def test_a_delayed_fire_carries_its_fire_at(fake_storage_provider, monkeypatch):
    fire_at = tick(15)
    await fake_storage_provider.get_storage(Trigger).create(Trigger(
        id="tr-cron", slug="release-reminder", name="r", config=DelayedTriggerConfig(fire_at=fire_at), enabled=True,
        next_fire_at=fire_at, created_at=tick(1),
    ))

    rendered = await _fire_for_real(fake_storage_provider, monkeypatch, now=NOW)

    assert rendered == [fire_at.isoformat()]
