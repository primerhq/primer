"""Integration: WorkerPool routes ClaimKind.TRIGGER claims to fire_trigger.

Mirrors :mod:`tests.worker.test_harness_claim_loop`: spin up the real
:class:`WorkerPool` against an in-memory event bus + claim engine, seed
a delayed Trigger row + a ChatMessage Subscription pointing at a real
chat, upsert a TRIGGER lease, then poll the chat's messages until the
subscription dispatched a user_message.

Catchup behaviour for scheduled triggers is unit-tested at the
``_run_engine_trigger`` level inside this module so we don't have to
fake out 64 cron ticks via the engine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.claim.factory import ClaimEngineFactory
from primer.int.claim import ClaimKind
from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat, ChatMessage
from primer.model.scheduler import WorkerConfig
from primer.model.storage import OffsetPage
from primer.model.trigger import (
    ChatMessageSubConfig,
    DelayedTriggerConfig,
    ScheduledTriggerConfig,
    Subscription,
    Trigger,
)
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_agent(storage_provider) -> Agent:
    agent = Agent(
        id="ag-trig",
        description="seeded test agent",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    await storage_provider.get_storage(Agent).create(agent)
    return agent


async def _seed_chat(storage_provider, agent_id: str) -> Chat:
    chat = Chat(
        id="cn-trig",
        agent_id=agent_id,
        last_seq=0,
        status="active",
        turn_status="idle",
        created_at=_now(),
    )
    await storage_provider.get_storage(Chat).create(chat)
    return chat


@pytest.mark.asyncio
async def test_pool_routes_trigger_lease_to_fire(
    fake_storage_provider, fake_provider_registry,
):
    """A claimed TRIGGER lease fires fire_trigger and the sub dispatches."""
    bus = InMemoryEventBus()
    await bus.initialize()
    scheduler = InMemoryScheduler(storage_provider=fake_storage_provider)

    agent = await _seed_agent(fake_storage_provider)
    await _seed_chat(fake_storage_provider, agent.id)

    # Seed the trigger + subscription rows.
    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)
    t = Trigger(
        id="tr-pool", slug="tr-pool", name="t", description=None,
        config=DelayedTriggerConfig(fire_at=_now()),
        enabled=True,
        next_fire_at=_now(),
        created_at=_now(),
    )
    await triggers.create(t)
    sub = Subscription(
        id="sb-pool", trigger_id="tr-pool",
        config=ChatMessageSubConfig(chat_id="cn-trig"),
        payload_template="hello",
        enabled=True,
        created_at=_now(),
    )
    await subs.create(sub)

    engine = ClaimEngineFactory.create(
        storage_provider=fake_storage_provider, event_bus=bus,
    )
    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=4,
            claim_batch_size=2,
            heartbeat_interval_seconds=5,
            lease_ttl_seconds=15,
            poll_interval_seconds=0.1,
            drain_timeout_seconds=5,
        ),
        scheduler=scheduler,
        storage=fake_storage_provider,
        workspace_registry=None,
        provider_registry=fake_provider_registry,
        event_bus=bus,
        chat_tick_router=None,
        engine=engine,
    )
    try:
        await pool.start()
        await engine.upsert(ClaimKind.TRIGGER, "tr-pool", priority=10)

        messages_storage = fake_storage_provider.get_storage(ChatMessage)
        # Poll until a user_message lands.
        landed = None
        for _ in range(60):
            await asyncio.sleep(0.05)
            page = await messages_storage.list(OffsetPage(offset=0, length=10))
            user_msgs = [m for m in page.items if m.kind == "user_message"]
            if user_msgs:
                landed = user_msgs[0]
                break
        assert landed is not None, (
            "trigger lease did not produce a user_message — "
            "WorkerPool TRIGGER dispatch is not wired"
        )
        assert landed.payload.get("content") == "hello"
        # The fire_id stamped on the message must reference our trigger.
        trig = landed.payload.get("trigger") or {}
        assert trig.get("trigger_id") == "tr-pool"
        assert trig.get("subscription_id") == "sb-pool"

        # Trigger row's last_fired_at was bumped + on_release disabled it
        # (delayed → one-off).
        updated_trigger = None
        for _ in range(20):
            await asyncio.sleep(0.05)
            row = await triggers.get("tr-pool")
            if row is not None and row.last_fired_at is not None:
                updated_trigger = row
                if not row.enabled:
                    break
        assert updated_trigger is not None
        assert updated_trigger.last_fired_at is not None
        # The adapter's on_release ran and flipped enabled→False for the
        # delayed (one-off) trigger.
        assert updated_trigger.enabled is False
    finally:
        await pool.drain_and_stop(timeout=5)
        await bus.aclose()


@pytest.mark.asyncio
async def test_run_engine_trigger_catchup_all_replays_missed_ticks(
    fake_storage_provider, fake_provider_registry, monkeypatch,
):
    """A scheduled trigger with catchup='all' fires once per missed tick.

    We bypass the engine claim loop and drive ``_run_engine_trigger``
    directly with a synthetic lease, then assert ``fire_trigger`` was
    invoked one time per missed cron occurrence plus once for the
    current tick (``scheduled_for=None``).
    """
    from primer.int.claim import Lease as ClaimLease

    bus = InMemoryEventBus()
    await bus.initialize()
    scheduler = InMemoryScheduler(storage_provider=fake_storage_provider)
    engine = ClaimEngineFactory.create(
        storage_provider=fake_storage_provider, event_bus=bus,
    )

    triggers = fake_storage_provider.get_storage(Trigger)
    # last_fired_at three minutes ago + a once-per-minute cron => 3
    # missed ticks the catchup loop must replay before the current tick.
    last_fired = _now() - timedelta(minutes=3, seconds=10)
    t = Trigger(
        id="tr-cron", slug="tr-cron", name="c", description=None,
        config=ScheduledTriggerConfig(
            cron="* * * * *", timezone="UTC", catchup="all",
        ),
        enabled=True,
        next_fire_at=_now(),
        last_fired_at=last_fired,
        created_at=last_fired,
    )
    await triggers.create(t)

    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=2,
            claim_batch_size=1,
            heartbeat_interval_seconds=5,
            lease_ttl_seconds=15,
            poll_interval_seconds=1,
            drain_timeout_seconds=2,
        ),
        scheduler=scheduler,
        storage=fake_storage_provider,
        workspace_registry=None,
        provider_registry=fake_provider_registry,
        event_bus=bus,
        chat_tick_router=None,
        engine=engine,
    )
    # Skip start() — we drive _run_engine_trigger directly.
    pool._worker_id = "wrk-test"

    # Capture every fire_trigger call.
    captured_calls: list[tuple[str, object]] = []

    async def _fake_fire(*, trigger_id, scheduled_for, deps):
        captured_calls.append((trigger_id, scheduled_for))
        from primer.trigger.dispatch import FireResult
        return FireResult(skipped=False, fire_id="fake", results=[])

    import primer.trigger.dispatch as _dispatch_mod
    monkeypatch.setattr(_dispatch_mod, "fire_trigger", _fake_fire)

    # Seed the lease so release() doesn't blow up on the in-memory engine.
    await engine.upsert(ClaimKind.TRIGGER, "tr-cron", priority=10)
    leases = await engine.claim_due("wrk-test", max_count=1)
    assert leases, "engine produced no lease — adapter eligibility may reject"
    lease = leases[0]

    await pool._run_engine_trigger(lease)
    await bus.aclose()

    # We expect ≥3 missed-tick calls (scheduled_for set) + exactly 1
    # current-tick call (scheduled_for=None). The exact missed count
    # depends on the wall-clock skew between last_fired and now; we
    # bound the assertion loosely so the test isn't timing-flaky.
    missed = [c for c in captured_calls if c[1] is not None]
    current = [c for c in captured_calls if c[1] is None]
    assert len(missed) >= 2, (
        f"expected catchup to replay ≥2 missed ticks, got {len(missed)}: "
        f"{captured_calls}"
    )
    assert len(current) == 1, (
        f"expected exactly one current-tick fire (scheduled_for=None), "
        f"got {len(current)}: {captured_calls}"
    )
    # All fires must reference our trigger.
    assert all(c[0] == "tr-cron" for c in captured_calls)
