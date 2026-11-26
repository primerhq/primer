"""Review 2026-11-23 finding 1.2: with the in-memory claim engine, a process restart
loses the TRIGGER and HARNESS leases, so a cron trigger never fires again and a harness
with an operation in flight stays wedged (every later operation answers 409).

Boot recovery re-arms SESSION leases (``recover_sessions``) and, since this finding's
fix, TRIGGER and HARNESS leases (``recover_trigger_and_harness_leases``), in that order.
An ``InMemoryClaimEngine`` holds its leases in memory, so a fresh engine over the same
storage has none: the trigger row survives the restart (with its ``next_fire_at``) and a
harness row survives with its ``pending_operation``, but their leases do not. These tests
build the app's in-memory shape (SQLite storage + ``InMemoryClaimEngine`` + the
TRIGGER/HARNESS adapters), arm the leases on a first engine, simulate the restart with a
NEW engine over the SAME storage, run the boot recovery the app runs, and assert the
leases are claimable afterwards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.api._app_lifespan_phases import (
    recover_sessions,
    recover_trigger_and_harness_leases,
)
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.harness import Harness, HarnessOperation, HarnessStatus
from primer.model.provider import SqliteConfig
from primer.model.trigger import ScheduledTriggerConfig, Trigger
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger import service


class _FakeScheduler:
    """The recovery only calls ``enqueue`` for live session rows; these tests seed none."""

    async def enqueue(self, sid: str) -> None:
        pass


def _engine(sp) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(adapters={
        ClaimKind.TRIGGER: TriggerClaimAdapter(storage=sp.get_storage(Trigger)),
        ClaimKind.HARNESS: HarnessClaimAdapter(
            harness_storage=sp.get_storage(Harness),
        ),
    })


async def _boot_recovery(engine: InMemoryClaimEngine, sp) -> None:
    """The app's real boot recovery sequence, in the order the lifespan runs it."""
    await recover_sessions(engine, _FakeScheduler(), sp)
    await recover_trigger_and_harness_leases(engine, sp)


@pytest.mark.asyncio
async def test_a_restart_keeps_the_cron_trigger_lease_armed(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "restart.sqlite")))
    await provider.initialize()
    try:
        # First "process": the app's in-memory claim engine over this storage.
        engine = _engine(provider)
        trigger = await service.create_trigger(
            slug="cron-restart",
            name="cron-restart",
            description=None,
            config=ScheduledTriggerConfig(cron="* * * * *", timezone="UTC"),
            enabled=True,
            owner=None,
            deps=service.ServiceDeps(storage_provider=provider, claim_engine=engine),
        )
        assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id), \
            "the premise: creating the scheduled trigger arms its lease"
        # The tick has passed and nothing fired before the restart: make it due so a
        # worker's claim would actually fire it (a future next_fire_at is not yet due).
        trigger_storage = provider.get_storage(Trigger)
        stored = await trigger_storage.get(trigger.id)
        await trigger_storage.update(stored.model_copy(update={
            "next_fire_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        }))

        # The restart: a NEW in-memory engine over the SAME storage, then the
        # boot recovery the app runs on startup.
        restarted = _engine(provider)
        await _boot_recovery(restarted, provider)

        # The trigger row persisted its next tick; its lease must too, and a worker
        # must be able to claim it (otherwise the cron trigger never fires again).
        stored = await trigger_storage.get(trigger.id)
        assert stored is not None and stored.next_fire_at is not None
        assert await restarted.has_lease(ClaimKind.TRIGGER, trigger.id), \
            "after the simulated restart the TRIGGER lease is gone: boot recovery " \
            "must re-arm it so a cron trigger on the in-memory claim engine fires again"
        claimed = await restarted.claim_due("w", max_count=5, kinds=[ClaimKind.TRIGGER])
        assert [lease.entity_id for lease in claimed] == [trigger.id], \
            "the re-armed trigger lease is not claimable after the restart"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_a_restart_keeps_the_harness_lease_claimable(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "harness.sqlite")))
    await provider.initialize()
    try:
        harness_storage = provider.get_storage(Harness)
        now = datetime.now(timezone.utc)
        # First "process": a harness with an operation in flight (its lease is armed
        # when the operation was enqueued).
        engine = _engine(provider)
        harness = await harness_storage.create(Harness(
            id="h1", slug="acme", name="Acme", git_url="https://github.com/x/y",
            created_at=now, status=HarnessStatus.READY,
            pending_operation=HarnessOperation.SYNC,
        ))
        await engine.upsert(
            ClaimKind.HARNESS, harness.id,
        )
        assert await engine.has_lease(ClaimKind.HARNESS, harness.id), \
            "the premise: the enqueued harness operation armed its lease"

        # The restart: a NEW engine over the SAME storage, then the boot recovery.
        restarted = _engine(provider)
        await _boot_recovery(restarted, provider)

        # The harness row kept its pending_operation; its lease must be re-armed and
        # claimable, so the in-flight operation is run again (it is not stuck at 409).
        claimed = await restarted.claim_due("w", max_count=5, kinds=[ClaimKind.HARNESS])
        assert [lease.entity_id for lease in claimed] == [harness.id], \
            "after the simulated restart the HARNESS lease is gone: boot recovery must " \
            "re-arm it so a pending harness operation is claimed again"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_a_disabled_trigger_gets_no_lease(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "disabled.sqlite")))
    await provider.initialize()
    try:
        now = datetime.now(timezone.utc)
        # A stale row: disabled but still carrying a (due) next_fire_at. It must not
        # be re-armed: the source would refuse to fire a disabled trigger anyway.
        trigger_storage = provider.get_storage(Trigger)
        await trigger_storage.create(Trigger(
            id="tr-disabled", slug="disabled", name="d",
            config=ScheduledTriggerConfig(cron="* * * * *", timezone="UTC"),
            enabled=False,
            next_fire_at=now - timedelta(minutes=1),
            created_at=now - timedelta(days=1),
        ))
        restarted = _engine(provider)
        await _boot_recovery(restarted, provider)
        assert not await restarted.has_lease(ClaimKind.TRIGGER, "tr-disabled"), \
            "a disabled trigger must not get a boot-recovery lease"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_recovery_is_idempotent_over_an_existing_lease(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "idem.sqlite")))
    await provider.initialize()
    try:
        engine = _engine(provider)
        trigger = await service.create_trigger(
            slug="cron-idem",
            name="cron-idem",
            description=None,
            config=ScheduledTriggerConfig(cron="* * * * *", timezone="UTC"),
            enabled=True,
            owner=None,
            deps=service.ServiceDeps(storage_provider=provider, claim_engine=engine),
        )
        # A persistent engine (Postgres) already holds the lease: running the
        # recovery over it must re-prioritise, never duplicate or clobber it.
        await recover_trigger_and_harness_leases(engine, provider)
        await recover_trigger_and_harness_leases(engine, provider)
        keys = [key for key in engine._leases  # noqa: SLF001
                if key == (ClaimKind.TRIGGER, trigger.id)]
        assert len(keys) == 1, "recovery must not duplicate an existing lease"
        assert await engine.has_lease(ClaimKind.TRIGGER, trigger.id)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_the_lifespan_runs_the_recovery_phase(tmp_path):
    """Round 1: every other test in this file calls the phase functions
    directly (the ``_boot_recovery`` helper), so deleting the lifespan's own
    call (``primer/api/_app_lifespan.py``) would leave them all green. This
    test boots the REAL lifespan twice over one SQLite file: the second
    boot's lifespan must arm the leases the persisted rows need - an enabled
    scheduled trigger with a due ``next_fire_at`` and a harness with a
    pending operation - with no test-side call to the phase in between.

    API-only mode + an explicit in-memory scheduler config: the claim engine
    is wired (recovery needs it) but no worker pool runs, so nothing claims
    the recovered leases out from under the assertion.
    """
    from pathlib import Path

    from primer.api.app import create_app
    from primer.api.config import AppConfig
    from primer.model.provider import StorageProviderConfig, StorageProviderType
    from primer.model.scheduler import (
        InMemorySchedulerConfig,
        RuntimeMode,
        SchedulerProviderConfig,
        SchedulerProviderType,
    )

    def _cfg(db_path: Path) -> AppConfig:
        return AppConfig(
            runtime_mode=RuntimeMode.API,
            auto_bootstrap=True,
            scheduler=SchedulerProviderConfig(
                provider=SchedulerProviderType.IN_MEMORY,
                config=InMemorySchedulerConfig(),
            ),
            db=StorageProviderConfig(
                provider=StorageProviderType.SQLITE,
                config=SqliteConfig(path=str(db_path)),
            ),
        )

    db_path = tmp_path / "lifespan-wiring.sqlite"
    now = datetime.now(timezone.utc)

    # ---- Boot #1: the app's real lifespan; seed the two persisted rows ----
    app1 = create_app(_cfg(db_path))
    async with app1.router.lifespan_context(app1):
        sp = app1.state.storage_provider
        await sp.get_storage(Trigger).create(Trigger(
            id="trg-life", slug="life-cron", name="life cron",
            config=ScheduledTriggerConfig(cron="* * * * *", timezone="UTC"),
            enabled=True, next_fire_at=now, created_at=now,
        ))
        await sp.get_storage(Harness).create(Harness(
            id="harn-life", slug="life", name="Life",
            status=HarnessStatus.READY, pending_operation=HarnessOperation.SYNC,
            created_at=now,
        ))

    # ---- Boot #2: same DB, a fresh in-memory engine, the real lifespan ----
    app2 = create_app(_cfg(db_path))
    async with app2.router.lifespan_context(app2):
        engine = app2.state.claim_engine
        assert engine is not None, "the claim engine must be wired in API mode with a scheduler"
        assert await engine.has_lease(ClaimKind.TRIGGER, "trg-life"), (
            "the lifespan did not run the trigger half of the recovery phase"
        )
        assert await engine.has_lease(ClaimKind.HARNESS, "harn-life"), (
            "the lifespan did not run the harness half of the recovery phase"
        )
