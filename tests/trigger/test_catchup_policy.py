"""A claimed scheduled trigger fires its due tick exactly once and catches up per its policy (ticket 01a16ddb, ruling 3).

The policy (``docs/dev/subsystems/triggers.md``, ``catchup`` on a ``scheduled`` trigger): ``none`` skips the missed ticks, ``one`` fires one
catch-up for them (the most recent missed tick), ``all`` fires each missed tick once (at most 64, oldest first); in every policy the due tick (the
latest tick at the claim) fires exactly once. A missed tick is one the platform did not fire when it came: from the trigger's ``next_fire_at``
(the first tick no claim has fired) up to the due tick. Before, ``all`` replayed from ``last_fired_at`` THROUGH the due tick and then fired the
current tick again (every tick twice, even with no downtime), and ``one`` and ``none`` both fired once with no catch-up.

Driven through the worker's trigger handler (``run_engine_trigger``) on a fake clock (``primer.worker.engine_handlers._now``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import primer.trigger.dispatch as dispatch_mod
from primer.model.trigger import ScheduledTriggerConfig, Trigger
from primer.trigger.dispatch import FireResult
from primer.worker.engine_handlers import run_engine_trigger

DAILY = "0 9 * * *"


def tick(day: int) -> datetime:
    """The daily 09:00 UTC tick of 2026-01-``day``."""
    return datetime(2026, 1, day, 9, 0, tzinfo=timezone.utc)


#: The fake clock: five seconds after the 09:00 tick of 2026-01-15.
NOW = tick(15) + timedelta(seconds=5)


def _scheduled(catchup: str, *, next_fire_at: datetime, last_fired_at: datetime | None, cron: str = DAILY) -> Trigger:
    return Trigger(
        id="tr-cron", slug="daily-digest", name="d", config=ScheduledTriggerConfig(cron=cron, timezone="UTC", catchup=catchup),
        enabled=True, next_fire_at=next_fire_at, last_fired_at=last_fired_at, created_at=tick(1),
    )


async def _claim(sp, monkeypatch, *, now: datetime = NOW) -> list:
    """Run the worker's handler on a claim of ``tr-cron`` at ``now``; the ``scheduled_for`` of every fire it makes, in order."""
    fired: list = []

    async def _fake_fire(*, trigger_id, scheduled_for, deps, extra_context=None):
        fired.append(scheduled_for)
        return FireResult(fire_id=f"fire-{len(fired)}", results=[])

    monkeypatch.setattr(dispatch_mod, "fire_trigger", _fake_fire)
    monkeypatch.setattr("primer.worker.engine_handlers._now", lambda: now, raising=False)
    released: list = []

    async def _release(lease, outcome) -> None:
        released.append(outcome)

    pool = SimpleNamespace(_storage=sp, _engine=None, _scheduler=None, _workspace_registry=None, _event_bus=None, _release_lease=_release)
    await run_engine_trigger(pool, SimpleNamespace(entity_id="tr-cron"))  # type: ignore[arg-type]
    assert len(released) == 1 and released[0].success, released
    return fired


@pytest.mark.parametrize("catchup", ["none", "one", "all"])
async def test_an_on_time_claim_fires_the_due_tick_once_in_every_policy(fake_storage_provider, monkeypatch, catchup):
    """No downtime: the claim for 2026-01-15 09:00 fires that tick once (``all`` used to fire it twice: once replayed, once as "now")."""
    await fake_storage_provider.get_storage(Trigger).create(
        _scheduled(catchup, next_fire_at=tick(15), last_fired_at=tick(14) + timedelta(seconds=2)),
    )

    assert await _claim(fake_storage_provider, monkeypatch) == [tick(15)]


@pytest.mark.parametrize(("catchup", "fires"), [
    pytest.param("none", [tick(15)], id="none skips the missed ticks"),
    pytest.param("one", [tick(14), tick(15)], id="one fires one catch-up (the latest missed tick)"),
    pytest.param("all", [tick(11), tick(12), tick(13), tick(14), tick(15)], id="all fires each missed tick once"),
])
async def test_after_downtime_the_missed_ticks_fire_per_the_policy_then_the_due_tick_once(fake_storage_provider, monkeypatch, catchup, fires):
    """Down from just after the 2026-01-10 fire until 2026-01-15 09:00:05: the ticks of the 11th to the 14th were missed, the 15th's is due."""
    await fake_storage_provider.get_storage(Trigger).create(
        _scheduled(catchup, next_fire_at=tick(11), last_fired_at=tick(10) + timedelta(seconds=2)),
    )

    assert await _claim(fake_storage_provider, monkeypatch) == fires


@pytest.mark.parametrize("catchup", ["none", "one", "all"])
async def test_a_trigger_enabled_again_does_not_catch_up_the_days_it_was_disabled(fake_storage_provider, monkeypatch, catchup):
    """Disabled after the 2026-01-08 fire and enabled again on the 14th: ``next_fire_at`` was recomputed on enable, so nothing was missed."""
    await fake_storage_provider.get_storage(Trigger).create(
        _scheduled(catchup, next_fire_at=tick(15), last_fired_at=tick(8) + timedelta(seconds=2)),
    )

    assert await _claim(fake_storage_provider, monkeypatch) == [tick(15)]


async def test_all_replays_at_most_64_missed_ticks_oldest_first(fake_storage_provider, monkeypatch):
    every_minute_due = NOW.replace(second=0)
    await fake_storage_provider.get_storage(Trigger).create(_scheduled(
        "all", cron="* * * * *", next_fire_at=every_minute_due - timedelta(minutes=100), last_fired_at=None,
    ))

    fired = await _claim(fake_storage_provider, monkeypatch)

    assert fired[-1] == every_minute_due
    missed = fired[:-1]
    assert missed == [every_minute_due - timedelta(minutes=100 - i) for i in range(64)]


async def test_a_worker_clock_a_little_behind_still_fires_the_tick_it_was_claimed_for(fake_storage_provider, monkeypatch):
    """The claim is for ``next_fire_at``; a worker whose clock reads two seconds before it fires that tick, not the day before's again."""
    await fake_storage_provider.get_storage(Trigger).create(
        _scheduled("one", next_fire_at=tick(15), last_fired_at=tick(14) + timedelta(seconds=2)),
    )

    assert await _claim(fake_storage_provider, monkeypatch, now=tick(15) - timedelta(seconds=2)) == [tick(15)]


# ---------------------------------------------------------------------------
# A claim delivered again after a lost release fires only what it did not fire (review of #915)
# ---------------------------------------------------------------------------


async def _run_for_real(sp, monkeypatch, *, now: datetime) -> list[str]:
    """One claim through the real ``fire_trigger`` whose release is LOST (the stub records nothing, so ``next_fire_at`` does not move); the
    ``scheduled_for`` of each delivery it made."""
    from primer.model.trigger import SessionAppendSubConfig, Subscription
    from primer.trigger.subscribers import SubscriptionDispatchResult

    delivered: list[str] = []

    class _Records:
        async def dispatch(self, sub, *, rendered_payload, **_kw):
            delivered.append(rendered_payload)
            return SubscriptionDispatchResult(ok=True, artefact_id="sess-1")

    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: _Records())
    monkeypatch.setattr("primer.worker.engine_handlers._now", lambda: now, raising=False)
    if await sp.get_storage(Subscription).get("sb-1") is None:
        await sp.get_storage(Subscription).create(Subscription(
            id="sb-1", trigger_id="tr-cron", config=SessionAppendSubConfig(session_id="sess-1"), payload_template="{{ scheduled_for }}",
            created_at=tick(1),
        ))

    async def _lost(lease, outcome) -> None:
        return None

    pool = SimpleNamespace(_storage=sp, _engine=None, _scheduler=None, _workspace_registry=None, _event_bus=None, _release_lease=_lost)
    await run_engine_trigger(pool, SimpleNamespace(entity_id="tr-cron"))  # type: ignore[arg-type]
    return delivered


@pytest.mark.parametrize(("catchup", "fires"), [
    pytest.param("none", [15], id="none"),
    pytest.param("one", [14, 15], id="one"),
    pytest.param("all", [11, 12, 13, 14, 15], id="all"),
])
async def test_a_claim_delivered_again_after_a_lost_release_fires_nothing_again(fake_storage_provider, monkeypatch, catchup, fires):
    """The worker fired every tick and died before its release: the claim comes back with ``next_fire_at`` unchanged and plans the same ticks.
    ``last_fired_id`` names the due tick, so none of them fires again (the catch-ups used to: only the due tick was deduplicated)."""
    await fake_storage_provider.get_storage(Trigger).create(
        _scheduled(catchup, next_fire_at=tick(11), last_fired_at=tick(10) + timedelta(seconds=2)),
    )

    first = await _run_for_real(fake_storage_provider, monkeypatch, now=NOW)
    again = await _run_for_real(fake_storage_provider, monkeypatch, now=NOW + timedelta(seconds=30))

    assert first == [tick(day).isoformat() for day in fires]
    assert again == []


async def test_a_claim_delivered_again_after_dying_part_way_fires_the_ticks_it_had_not_reached(fake_storage_provider, monkeypatch):
    """The worker fired the 11th and 12th of an ``all`` catch-up and died: the claim comes back and fires the 13th, 14th and 15th only."""
    from primer.trigger.fire_id import make_fire_id

    row = _scheduled("all", next_fire_at=tick(11), last_fired_at=tick(12))
    row.last_fired_id = make_fire_id("tr-cron", tick(12))
    await fake_storage_provider.get_storage(Trigger).create(row)

    again = await _run_for_real(fake_storage_provider, monkeypatch, now=NOW)

    assert again == [tick(13).isoformat(), tick(14).isoformat(), tick(15).isoformat()]
