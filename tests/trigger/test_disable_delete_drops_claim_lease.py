"""A disabled or deleted trigger leaves no claim lease (ticket 01a16ede).

The claim loop reads the stored row of every armed lease on every poll and decides eligibility from it. A disabled trigger's lease is
ineligible forever, and a deleted trigger's lease outlives its row: without a ``delete_lease`` on the way out, both keep paying a per-poll
read for nothing (the probe p7 of the review of #928 showed the reads). ``update_trigger(enabled=False)`` and ``delete_trigger`` must
therefore call the claim engine's ``delete_lease`` (it is a no-op when the lease is missing, and works on both engines), and a re-enable
re-arms the lease via the usual upsert.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.trigger import ScheduledTriggerConfig, Trigger
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger import service


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "lease.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _engine(sp) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(adapters={
        ClaimKind.TRIGGER: TriggerClaimAdapter(storage=sp.get_storage(Trigger)),
    })


def _cron() -> ScheduledTriggerConfig:
    return ScheduledTriggerConfig(cron="* * * * *", timezone="UTC")


def _deps(sp, engine) -> service.ServiceDeps:
    return service.ServiceDeps(storage_provider=sp, claim_engine=engine)


async def _make(sp, engine, slug: str) -> Trigger:
    return await service.create_trigger(
        slug=slug, name=slug, description=None, config=_cron(), enabled=True, owner=None, deps=_deps(sp, engine),
    )


async def test_a_disabled_trigger_leaves_no_claim_lease(sp):
    """Disabling the trigger drops its lease; the row stays, disabled, with no next tick."""
    engine = _engine(sp)
    trigger = await _make(sp, engine, "dis-arm")
    assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id)

    updated = await service.update_trigger(trigger_id=trigger.id, enabled=False, deps=_deps(sp, engine))

    assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id) is False
    assert updated.enabled is False and updated.next_fire_at is None
    stored = await sp.get_storage(Trigger).get(trigger.id)
    assert stored.enabled is False and stored.next_fire_at is None


async def test_a_deleted_trigger_leaves_no_claim_lease(sp):
    """Deleting the trigger drops its lease; nothing of it stays behind."""
    engine = _engine(sp)
    trigger = await _make(sp, engine, "go-away")
    assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id)

    await service.delete_trigger(trigger_id=trigger.id, deps=_deps(sp, engine))

    assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id) is False
    assert await sp.get_storage(Trigger).get(trigger.id) is None


async def test_re_enabling_a_disabled_trigger_re_arms_the_lease(sp):
    """The drop is not one-way: a re-enable re-arms the lease at the trigger's next tick."""
    engine = _engine(sp)
    trigger = await _make(sp, engine, "again")
    deps = _deps(sp, engine)
    await service.update_trigger(trigger_id=trigger.id, enabled=False, deps=deps)
    assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id) is False

    reenabled = await service.update_trigger(trigger_id=trigger.id, enabled=True, deps=deps)

    assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id) is True
    assert reenabled.next_fire_at is not None


async def test_a_trigger_created_disabled_never_arms_a_lease(sp):
    """The pin that already holds today: no lease is ever created for a disabled trigger."""
    engine = _engine(sp)
    trigger = await service.create_trigger(
        slug="born-off", name="born-off", description=None, config=_cron(), enabled=False, owner=None, deps=_deps(sp, engine),
    )
    assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id) is False
