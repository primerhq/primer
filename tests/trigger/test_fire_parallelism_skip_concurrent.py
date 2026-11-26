"""Review 2026-11-23 6.8: a parallelism='skip' fire is check-then-act, and the webhook path has no claim between the check and the create.

Every webhook POST drives ``fire_trigger`` from its own background task (``primer/api/routers/webhooks.py``): N concurrent deliveries of one trigger are N concurrent fires, and nothing upstream serializes them (the claim engine serializes only claim-DRIVEN fires, which webhook fires are not). Each ``parallelism='skip'`` fire reads "is a live session in flight for this subscription?" and creates one if it finds none. Concurrent passes that all read "no live session" before any of them has created all create: N sessions, the skip gate void.

This test drives ``fire_trigger`` the way the endpoint does (concurrent calls, no claim) and opens the TOCTOU window the in-memory fakes never open on their own (they never suspend on a read): the busy check is wrapped so it awaits one loop turn after its read (``asyncio.sleep(0)``), the same suspension every real storage round-trip provides, letting every concurrent pass pass the check before the first creates.

Old behaviour: 5 fires -> 5 sessions. Fixed: the check and the create run under one per-trigger lock, so the later fires see the first session and skip.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.model.principal import PrincipalRef
from primer.model.trigger import (
    AgentFreshSubConfig,
    Subscription,
    Trigger,
    WebhookTriggerConfig,
)
from primer.model.workspace_session import WorkspaceSession
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps

# A platform-owned subscription, as the other fire tests use one.
_PLATFORM = PrincipalRef.system()


def _now():
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_concurrent_webhook_fires_start_exactly_one_session_for_a_skip_subscription(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent, monkeypatch,
):
    """N concurrent fires of one trigger start exactly one session."""
    import primer.trigger.subscribers.agent_fresh_session as afs

    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)

    t = Trigger(
        id="tr-1", slug="tr-x", name="x", description=None,
        config=WebhookTriggerConfig(), enabled=True, created_at=_now(),
    )
    await triggers.create(t)
    sub = Subscription(owner=_PLATFORM,
        id="sb-1", trigger_id="tr-1",
        config=AgentFreshSubConfig(
            workspace_id=seeded_workspace.id, agent_id=seeded_agent.id,
        ),
        parallelism="skip", enabled=True, created_at=_now(),
    )
    await subs.create(sub)

    real_check = afs.check_subscription_busy

    async def busy_check_then_yield(sub_, deps, *, fire_context=None):
        # The read the real check performs, then the suspension a real
        # storage round-trip always provides (the fakes never suspend, so
        # without this the check-then-act window below never opens).
        result = await real_check(sub_, deps, fire_context=fire_context)
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(afs, "check_subscription_busy", busy_check_then_yield)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )

    # Five concurrent deliveries of the same webhook POST, the way the
    # endpoint runs them: one background task per request.
    results = await asyncio.gather(
        *(
            fire_trigger(
                trigger_id="tr-1", scheduled_for=None, deps=deps,
                extra_context={"webhook_body": f'{{"n": {i}}}'},
            )
            for i in range(5)
        )
    )

    fired = [
        s for s in sessions._data.values()  # noqa: SLF001
        if s.metadata.get("subscription_id") == "sb-1"
    ]
    assert len(fired) == 1, (
        f"concurrent fires raced the skip gate: {len(fired)} sessions created"
    )
    created = [
        r for res in results for r in res.results
        if r.get("artefact_id") and not r.get("skipped")
    ]
    assert len(created) == 1, (
        f"{len(created)} fires reported a created session; the skip gate "
        "must let exactly one of the concurrent passes through"
    )


# ---------------------------------------------------------------------------
# Round 1 (review 2026-11-23 lane J): M1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_lock_eviction_never_drops_a_held_lock(monkeypatch):
    """M1: the fire-lock map evicted by insertion order and could evict a
    HELD lock, so the next fire of that trigger got a fresh lock and two
    fires ran unserialized. Eviction skips held locks (oldest unlocked goes);
    if every entry is held, the oldest goes anyway (the map must stay
    bounded and the scan must terminate)."""
    import primer.trigger.dispatch as dispatch

    monkeypatch.setattr(dispatch, "_FIRE_LOCK_MAX_KEYS", 3)
    lock_a = dispatch._fire_lock("tr-a")
    dispatch._fire_lock("tr-b")
    dispatch._fire_lock("tr-c")
    await lock_a.acquire()  # tr-a's fire is in flight
    try:
        dispatch._fire_lock("tr-d")  # len 4 > 3: evict one
        dispatch._fire_lock("tr-e")  # len 4 > 3: evict another
        kept = dispatch._fire_locks.get("tr-a")
        assert kept is not None and kept[1] is lock_a, (
            f"the held fire lock was evicted (map: {sorted(dispatch._fire_locks)}): "
            "two fires of tr-a can run unserialized"
        )
        assert len(dispatch._fire_locks) <= 3

        # The kept lock still serializes: a concurrent fire of tr-a blocks.
        got = []

        async def second_fire():
            async with dispatch._fire_lock("tr-a"):
                got.append(True)

        task = asyncio.create_task(second_fire())
        await asyncio.sleep(0.01)
        assert not got, "a second fire of tr-a ran while the first held the lock"
        lock_a.release()
        await task
        assert got == [True]

        # Every lock held: the fallback evicts the oldest anyway (no hang).
        lock_d = dispatch._fire_lock("tr-d")
        lock_e = dispatch._fire_lock("tr-e")
        await lock_d.acquire()
        await lock_e.acquire()
        try:
            dispatch._fire_lock("tr-f")  # len 4 > 3, all held: oldest goes
            assert len(dispatch._fire_locks) <= 3
        finally:
            lock_d.release()
            lock_e.release()
    finally:
        if lock_a.locked():
            lock_a.release()
