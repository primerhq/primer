"""TriggerClaimAdapter — Spec §12.3.

Covers the unit-level surface: kind/entity_table, the eligibility SQL
predicate, and the on_release advancement logic for both delayed and
scheduled trigger kinds. The full integration path (claim engine →
worker pool → fire_trigger) is exercised in ``test_pool_trigger.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.trigger import (
    DelayedTriggerConfig,
    ScheduledTriggerConfig,
    Trigger,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_trigger_adapter_kind():
    a = TriggerClaimAdapter(storage=None)
    assert a.kind == ClaimKind.TRIGGER
    assert a.entity_table == "trigger"


def test_trigger_adapter_eligibility_sql_filters_time_kinds():
    """The predicate must reference the right JSONB paths and gate by
    enabled + next_fire_at + supported kinds."""
    a = TriggerClaimAdapter(storage=None)
    sql = a.eligibility_sql()
    assert "next_fire_at" in sql
    assert "enabled" in sql
    assert "delayed" in sql
    assert "scheduled" in sql
    # ``config.kind`` is nested under data->'config'->>'kind' — not a
    # top-level field — so the SQL must walk through 'config' first.
    assert "data->'config'->>'kind'" in sql


@pytest.mark.asyncio
async def test_on_release_disables_delayed_trigger(fake_storage_provider):
    """One-off delayed triggers auto-disable post-fire."""
    storage = fake_storage_provider.get_storage(Trigger)
    t = Trigger(
        id="tr-1", slug="tr-x", name="x", description=None,
        config=DelayedTriggerConfig(fire_at=_now()),
        enabled=True,
        next_fire_at=_now(),
        last_fired_at=_now(),
        created_at=_now(),
    )
    await storage.create(t)

    adapter = TriggerClaimAdapter(storage=storage)
    await adapter.on_release(
        conn=None, entity_id="tr-1",
        outcome=ReleaseOutcome(success=True, drop_lease=False),
    )
    updated = await storage.get("tr-1")
    assert updated.enabled is False
    assert updated.next_fire_at is None


@pytest.mark.asyncio
async def test_on_release_advances_scheduled_trigger(fake_storage_provider):
    """Scheduled triggers advance to the next future cron occurrence."""
    storage = fake_storage_provider.get_storage(Trigger)
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t = Trigger(
        id="tr-2", slug="tr-y", name="y", description=None,
        # Every minute — guaranteed a next occurrence after ``now``.
        config=ScheduledTriggerConfig(cron="* * * * *", timezone="UTC"),
        enabled=True,
        next_fire_at=base,
        last_fired_at=base,
        created_at=base,
    )
    await storage.create(t)

    adapter = TriggerClaimAdapter(storage=storage)
    before = datetime.now(timezone.utc)
    await adapter.on_release(
        conn=None, entity_id="tr-2",
        outcome=ReleaseOutcome(success=True, drop_lease=False),
    )
    updated = await storage.get("tr-2")
    assert updated.enabled is True
    assert updated.next_fire_at is not None
    # Next fire must be strictly after the moment on_release ran and
    # within a couple of minutes (cron is once-per-minute).
    assert updated.next_fire_at > before
    assert updated.next_fire_at < before + timedelta(minutes=2)


@pytest.mark.asyncio
async def test_on_release_missing_trigger_is_noop(fake_storage_provider):
    """on_release on a missing entity must not raise."""
    storage = fake_storage_provider.get_storage(Trigger)
    adapter = TriggerClaimAdapter(storage=storage)
    # No exception expected.
    await adapter.on_release(
        conn=None, entity_id="tr-missing",
        outcome=ReleaseOutcome(success=False, drop_lease=True),
    )


def test_factory_registers_trigger_adapter(fake_storage_provider):
    """The claim factory must include the TRIGGER adapter."""
    from primer.bus.in_memory import InMemoryEventBus
    from primer.claim.factory import ClaimEngineFactory

    bus = InMemoryEventBus()
    engine = ClaimEngineFactory.create(
        storage_provider=fake_storage_provider, event_bus=bus,
    )
    # The in-memory engine stores adapters as a private dict; assert
    # via the public claim_due path is overkill, so just walk attrs.
    adapters = getattr(engine, "_adapters", {})
    assert ClaimKind.TRIGGER in adapters
    assert isinstance(adapters[ClaimKind.TRIGGER], TriggerClaimAdapter)
