"""A trigger claim never fires a future tick, and its lease comes back requeued to the next tick (ticket 01a16ddb, review of #915).

* An early claim (its tick more than ``CLOCK_SKEW``, 5 s, ahead of the worker's clock) fires nothing and hands the lease back untouched
  (``entity_noop``: the release would advance a scheduled trigger past the tick, or disable a delayed one, as if it had fired), requeued to
  the tick.
* A claim that fired is released requeued to the trigger's next tick; a trigger with no next tick (a delayed one that fired) drops its lease.
  Without that, the in-memory claim engine, which did not evaluate the adapter's eligibility then (it does since ticket 01a16e79), claimed
  a released trigger again at once: on a SQLite deployment every scheduled trigger re-fired in a loop, firing its future ticks early.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import primer.claim.adapters.triggers as adapter_mod
import primer.trigger.dispatch as dispatch_mod
from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.trigger import DelayedTriggerConfig, ScheduledTriggerConfig, Trigger
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger import service
from primer.trigger.dispatch import FireResult
from primer.worker.engine_handlers import run_engine_trigger

TICK = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)


def _trigger(kind: str, *, next_fire_at: datetime) -> Trigger:
    config = (
        ScheduledTriggerConfig(cron="0 9 * * *", timezone="UTC", catchup="all") if kind == "scheduled"
        else DelayedTriggerConfig(fire_at=next_fire_at)
    )
    return Trigger(id="tr-c", slug="claimed", name="c", config=config, enabled=True, next_fire_at=next_fire_at, created_at=TICK - timedelta(days=9))


async def _handle(sp, monkeypatch, *, now: datetime) -> tuple[list, list]:
    """Run the handler on a claim of ``tr-c`` at ``now``: (the ``scheduled_for`` of each fire, the release outcomes)."""
    fired: list = []

    async def _fake_fire(*, trigger_id, scheduled_for, deps, extra_context=None):
        fired.append(scheduled_for)
        return FireResult(fire_id="f", results=[])

    monkeypatch.setattr(dispatch_mod, "fire_trigger", _fake_fire)
    monkeypatch.setattr("primer.worker.engine_handlers._now", lambda: now)
    released: list = []

    async def _release(lease, outcome) -> None:
        released.append(outcome)

    pool = SimpleNamespace(_storage=sp, _engine=None, _scheduler=None, _workspace_registry=None, _event_bus=None, _release_lease=_release)
    await run_engine_trigger(pool, SimpleNamespace(entity_id="tr-c"))  # type: ignore[arg-type]
    return fired, released


@pytest.mark.parametrize("kind", ["scheduled", "delayed"])
async def test_an_early_claim_fires_nothing_and_hands_the_lease_back_requeued_to_its_tick(fake_storage_provider, monkeypatch, kind):
    await fake_storage_provider.get_storage(Trigger).create(_trigger(kind, next_fire_at=TICK))

    fired, [outcome] = await _handle(fake_storage_provider, monkeypatch, now=TICK - timedelta(minutes=1))

    assert fired == [], "a future tick was fired early"
    assert outcome.entity_noop is True, "the release would advance (or disable) a trigger that did not fire"
    assert outcome.drop_lease is False
    assert outcome.requeue_after == timedelta(minutes=1)
    row = await fake_storage_provider.get_storage(Trigger).get("tr-c")
    assert row.enabled is True and row.next_fire_at == TICK


@pytest.mark.parametrize("kind", ["scheduled", "delayed"])
async def test_a_claim_inside_the_skew_window_fires_its_tick(fake_storage_provider, monkeypatch, kind):
    await fake_storage_provider.get_storage(Trigger).create(_trigger(kind, next_fire_at=TICK))

    fired, [outcome] = await _handle(fake_storage_provider, monkeypatch, now=TICK - timedelta(seconds=4))

    assert fired == [TICK]
    assert outcome.entity_noop is False


async def test_a_scheduled_claim_that_fired_is_requeued_to_its_next_tick(fake_storage_provider, monkeypatch):
    await fake_storage_provider.get_storage(Trigger).create(_trigger("scheduled", next_fire_at=TICK))

    fired, [outcome] = await _handle(fake_storage_provider, monkeypatch, now=TICK + timedelta(seconds=5))

    assert fired == [TICK]
    assert outcome.drop_lease is False
    assert outcome.requeue_after == timedelta(days=1, seconds=-5), "requeued to tomorrow's 09:00"


async def test_a_delayed_claim_that_fired_drops_its_lease(fake_storage_provider, monkeypatch):
    await fake_storage_provider.get_storage(Trigger).create(_trigger("delayed", next_fire_at=TICK))

    fired, [outcome] = await _handle(fake_storage_provider, monkeypatch, now=TICK + timedelta(seconds=5))

    assert fired == [TICK]
    assert outcome.drop_lease is True and outcome.requeue_after is None


# ---------------------------------------------------------------------------
# On SQLite (the in-memory claim engine), end to end
# ---------------------------------------------------------------------------


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "claims.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _on_a_fake_clock(monkeypatch, sp, start: datetime, *, engine_ahead: timedelta = timedelta(0)):
    """``engine_ahead``: how far the claimer's clock (the engine's; on Postgres, the database's) runs ahead of the worker's."""
    clock = {"now": start}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    # Every clock the claim reads: the engine's, the handler's and the release's (TriggerClaimAdapter.on_release).
    monkeypatch.setattr(adapter_mod, "datetime", _Clock)
    monkeypatch.setattr("primer.worker.engine_handlers._now", lambda: clock["now"])
    engine = InMemoryClaimEngine(
        adapters={ClaimKind.TRIGGER: TriggerClaimAdapter(storage=sp.get_storage(Trigger))}, clock=lambda: clock["now"] + engine_ahead,
    )

    async def _release(lease, outcome) -> None:
        await engine.release(lease, outcome=outcome)

    pool = SimpleNamespace(_storage=sp, _engine=engine, _scheduler=None, _workspace_registry=None, _event_bus=None, _release_lease=_release)
    return clock, engine, pool


async def _fired(sp) -> list[str]:
    events = await sp.get_event_store().read_after(0, limit=100, event_type_prefix="trigger.fired")
    return [event.payload["scheduled_for"] for event in events]


@pytest.mark.parametrize("catchup", ["none", "one", "all"])
async def test_on_sqlite_a_released_scheduled_trigger_is_not_claimed_again_before_its_next_tick(sp, monkeypatch, catchup):
    """The reviewer's loop: on the in-memory engine a released trigger was claimable at once, and the claim fired the NEXT tick early."""
    clock, engine, pool = _on_a_fake_clock(monkeypatch, sp, datetime.now(timezone.utc))
    made = await service.create_trigger(
        slug="hourly", name="h", description=None, config=ScheduledTriggerConfig(cron="0 * * * *", timezone="UTC", catchup=catchup),
        deps=service.ServiceDeps(storage_provider=sp, claim_engine=engine),
    )
    first = made.next_fire_at
    ticks = [first + timedelta(hours=hour) for hour in range(3)]

    for tick in ticks:
        clock["now"] = tick - timedelta(minutes=1)
        assert await engine.claim_due("w", max_count=5) == [], "claimable before its tick"
        clock["now"] = tick + timedelta(seconds=2)
        [lease] = await engine.claim_due("w", max_count=5)
        await run_engine_trigger(pool, lease)  # type: ignore[arg-type]
        assert await engine.claim_due("w", max_count=5) == [], "a released trigger was claimable again at once"
        assert (await sp.get_storage(Trigger).get(made.id)).next_fire_at == tick + timedelta(hours=1)

    assert await _fired(sp) == [tick.isoformat() for tick in ticks], "each tick fires once, on time"


async def test_on_sqlite_a_delayed_trigger_fires_once_and_leaves_no_lease(sp, monkeypatch):
    clock, engine, pool = _on_a_fake_clock(monkeypatch, sp, TICK - timedelta(hours=1))
    made = await service.create_trigger(
        slug="once", name="o", description=None, config=DelayedTriggerConfig(fire_at=TICK),
        deps=service.ServiceDeps(storage_provider=sp, claim_engine=engine),
    )
    clock["now"] = TICK + timedelta(seconds=1)
    [lease] = await engine.claim_due("w", max_count=5)

    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]

    assert await _fired(sp) == [TICK.isoformat()]
    assert not await engine.has_lease(ClaimKind.TRIGGER, made.id)
    assert (await sp.get_storage(Trigger).get(made.id)).enabled is False


async def test_on_sqlite_an_early_claim_is_requeued_to_its_tick(sp, monkeypatch):
    """An early claim fires nothing and is not claimable again until the tick.

    The claim is early because the claimer's clock runs ahead of the worker's by more than ``CLOCK_SKEW``: the engine finds the trigger due
    at its tick while the worker's clock reads ten minutes before it. That is how an early claim happens on Postgres (the query reads the
    database's ``now()``). Before ticket 01a16e79 the in-memory engine also claimed a lease upserted due now ahead of its tick, because it
    applied no eligibility; it now refuses that lease, as Postgres does."""
    clock, engine, pool = _on_a_fake_clock(monkeypatch, sp, TICK - timedelta(minutes=10), engine_ahead=timedelta(minutes=10))
    await sp.get_storage(Trigger).create(_trigger("scheduled", next_fire_at=TICK))
    await engine.upsert(ClaimKind.TRIGGER, "tr-c")
    [lease] = await engine.claim_due("w", max_count=5)

    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]

    assert await _fired(sp) == []
    assert await engine.claim_due("w", max_count=5) == []
    assert (await sp.get_storage(Trigger).get("tr-c")).next_fire_at == TICK, "the early claim moved the trigger"
    clock["now"] = TICK + timedelta(seconds=1)
    [lease] = await engine.claim_due("w", max_count=5)
    await run_engine_trigger(pool, lease)  # type: ignore[arg-type]
    assert await _fired(sp) == [TICK.isoformat()]
