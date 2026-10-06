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
import primer.trigger.dispatch as dispatch_module
from primer.trigger.dispatch import fire_trigger
from primer.trigger.fire_id import make_fire_id
from primer.trigger.subscribers import DispatchDeps, SubscriptionDispatchResult


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


class _ProcessDied(BaseException):
    """Stands for the worker dying inside a fan-out (a kill, a cancel): a BaseException, so the per-subscription
    isolation in ``fire_trigger`` (``except Exception``) does not turn it into an ``ok=False`` result."""


@pytest.mark.asyncio
async def test_the_dedup_marker_is_recorded_after_the_fan_out_so_a_crash_inside_it_is_redelivered(
    fake_storage_provider, fake_claim_engine, fake_scheduler, fake_workspace_registry, monkeypatch,
):
    """``last_fired_id`` is written AFTER every subscription was dispatched (see the comment in ``fire_trigger``), so
    a fire that dies half way leaves no marker and the redelivery of the same tick dispatches AGAIN, including to the
    subscriptions the first pass already served: at-least-once, not exactly-once. That is the documented best-effort
    v1 (docs/dev/subsystems/triggers.md); this pins it so a change of the marker's position is a decision, not an
    accident (before the dispatch it would lose the unserved subscriptions on a crash, the opposite trade)."""
    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)
    await triggers.create(Trigger(
        id="tr-1", slug="tr-x", name="x", description=None,
        config=ScheduledTriggerConfig(cron="0 * * * *", timezone="UTC"),
        enabled=True, next_fire_at=_now(), created_at=_now(),
    ))
    for sub_id in ("sb-1", "sb-2"):
        await subs.create(Subscription(
            id=sub_id, trigger_id="tr-1",
            config=AgentFreshSubConfig(workspace_id="ws-x", agent_id="ag-x"),
            parallelism="queue", enabled=True, created_at=_now(),
        ))

    delivered: list[str] = []
    calls = {"n": 0, "crash_on": 2}

    class _Dispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            calls["n"] += 1
            if calls["n"] == calls["crash_on"]:
                raise _ProcessDied()
            delivered.append(sub.id)
            return SubscriptionDispatchResult(ok=True, artefact_id=f"art-{sub.id}")

    monkeypatch.setattr(dispatch_module, "get_dispatcher", lambda kind: _Dispatcher())
    deps = DispatchDeps(
        storage_provider=fake_storage_provider, claim_engine=fake_claim_engine,
        scheduler=fake_scheduler, workspace_registry=fake_workspace_registry,
    )
    tick = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(_ProcessDied):
        await fire_trigger(trigger_id="tr-1", scheduled_for=tick, deps=deps)
    assert len(delivered) == 1, "precondition: the first subscription was served before the fire died"
    assert (await triggers.get("tr-1")).last_fired_id is None, "the marker was recorded before the fan-out finished"

    calls["crash_on"] = -1
    redelivery = await fire_trigger(trigger_id="tr-1", scheduled_for=tick, deps=deps)

    assert redelivery.skipped is False, "the redelivery was deduplicated against a fire that never finished"
    assert len(delivered) == 3, "every subscription is dispatched on the redelivery, the served one a second time"
    assert delivered[0] == delivered[1], "the subscription the first pass served was served again"
    assert (await triggers.get("tr-1")).last_fired_id == redelivery.fire_id, "a finished fire records its marker"
