"""fire_id idempotency: a redelivered logical fire must not double-fire.

Spec §12.6: ``fire_id`` is the deterministic correlation token for a
fire. At-least-once delivery (re-claim, catchup replay of the same
``scheduled_for``, duplicate event) can drive the SAME logical fire
through ``fire_trigger`` twice; the second pass must be a no-op so the
downstream side-effect (a fresh session) happens exactly once.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.trigger import (
    AgentFreshSubConfig,
    ScheduledTriggerConfig,
    Subscription,
    Trigger,
)
from primer.model.workspace_session import WorkspaceSession
from primer.trigger.dispatch import fire_trigger
from primer.trigger.fire_id import make_fire_id
from primer.trigger.subscribers import DispatchDeps


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_redelivered_fire_does_not_double_create_session(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent,
):
    """Firing the SAME scheduled tick twice creates only one session.

    Both calls carry the same ``scheduled_for`` so they resolve to the
    same ``fire_id``; the second must dedup to a skip.
    """
    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)

    t = Trigger(
        id="tr-1", slug="tr-x", name="x", description=None,
        config=ScheduledTriggerConfig(cron="0 * * * *", timezone="UTC"),
        enabled=True, next_fire_at=_now(), created_at=_now(),
    )
    await triggers.create(t)
    # parallelism="queue" so the busy-check never masks the double-fire;
    # any dedup MUST come from fire_id, not the skip branch.
    sub = Subscription(
        id="sb-1", trigger_id="tr-1",
        config=AgentFreshSubConfig(
            workspace_id=seeded_workspace.id, agent_id=seeded_agent.id,
        ),
        parallelism="queue", enabled=True, created_at=_now(),
    )
    await subs.create(sub)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )

    tick = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
    res1 = await fire_trigger(
        trigger_id="tr-1", scheduled_for=tick, deps=deps,
    )
    res2 = await fire_trigger(
        trigger_id="tr-1", scheduled_for=tick, deps=deps,
    )

    # Same logical tick -> identical fire_id on both passes.
    assert res1.fire_id == res2.fire_id

    # Exactly one session landed despite the redelivery.
    all_sessions = list(sessions._data.values())  # noqa: SLF001
    fired = [
        s for s in all_sessions
        if s.metadata.get("subscription_id") == "sb-1"
    ]
    assert len(fired) == 1, (
        f"redelivery double-fired: {len(fired)} sessions created"
    )

    # The second pass reports the duplicate as a skip, not a fresh fire.
    assert res2.skipped is True


def test_fire_id_is_stable_for_same_scheduled_tick():
    """``make_fire_id`` keyed on the scheduled instant is deterministic."""
    tick = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
    a = make_fire_id("tr-1", tick)
    b = make_fire_id("tr-1", tick)
    assert a == b


@pytest.mark.asyncio
async def test_skip_parallelism_serialized_busy_check(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent,
):
    """BUG 2 (TOCTOU on parallelism='skip') is NOT reproducible at dispatch.

    The claim engine holds exactly one lease per ``(kind, entity_id)``
    (``primer/claim/postgres.py`` ``ON CONFLICT (kind, entity_id)``;
    ``primer/claim/sql.py`` claims only rows where
    ``claimed_by IS NULL OR expires_at < now()`` with
    ``FOR UPDATE OF l SKIP LOCKED``). A trigger therefore fires under a
    single in-flight lease, and its subscriptions fan out SEQUENTIALLY
    inside ``fire_trigger``: two deliveries for the same subscription
    cannot run ``_check_subscription_busy`` concurrently.

    This test exercises the serial pattern the engine actually permits:
    a first skip-dispatch creates a running session, and an immediate
    second skip-dispatch observes it and skips. No double-run.
    """
    from primer.model.trigger import AgentFreshSubConfig
    from primer.trigger.subscribers.agent_fresh_session import (
        AgentFreshSessionDispatcher,
    )

    sub = Subscription(
        id="sb-1", trigger_id="tr-1",
        config=AgentFreshSubConfig(
            workspace_id=seeded_workspace.id, agent_id=seeded_agent.id,
        ),
        parallelism="skip", enabled=True, created_at=_now(),
    )
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    dispatcher = AgentFreshSessionDispatcher()

    res1 = await dispatcher.dispatch(
        sub, rendered_payload="x", fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-1", deps=deps,
    )
    res2 = await dispatcher.dispatch(
        sub, rendered_payload="x", fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-2", deps=deps,
    )

    assert res1.ok and not res1.skipped
    assert res2.ok and res2.skipped  # second sees the first running -> skip

    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    fired = [
        s for s in sessions._data.values()  # noqa: SLF001
        if s.metadata.get("subscription_id") == "sb-1"
    ]
    assert len(fired) == 1
