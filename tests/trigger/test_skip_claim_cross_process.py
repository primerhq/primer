"""X2.1: skip-policy fires serialized ACROSS processes.

A ``parallelism="skip"`` subscription's fire is a check-then-act (is a live
session in flight?) that was serialized by a per-process asyncio lock alone:
two API replicas delivering the same webhook burst each read "not busy" and
each created a session (review 2026-11-23 6.8, cross-process follow-up). The
fix adds a DB-level conditional claim on the subscription id (the row id is
derived from it, so two claims conflict on every backend, as
``claim_thread``'s do), held from the busy check through the session create;
the in-process lock stays as the fast path, narrowed to skip-policy
subscriptions.

"Two processes" here means two ``DispatchDeps`` bundles over the SAME storage
(the two replicas of one deployment share the store), or two storage
provider instances over one SQLite file (two real connections). The live
Postgres twin is ``tests/storage/test_conditional_claim_insert.py``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

import primer.trigger.dispatch as dispatch_mod
from primer.model.principal import PrincipalRef
from primer.model.trigger import (
    AgentFreshSubConfig,
    Subscription,
    Trigger,
    WebhookTriggerConfig,
)
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps, SubscriptionDispatchResult

# A platform-owned subscription, as the other fire tests use one.
_PLATFORM = PrincipalRef.system()


def _now():
    return datetime.now(timezone.utc)


async def _wait_until(pred, timeout=2.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def _seeded_skip_trigger_and_sub(fake_storage_provider, seeded_workspace, seeded_agent):
    async def _seed():
        triggers = fake_storage_provider.get_storage(Trigger)
        subs = fake_storage_provider.get_storage(Subscription)
        await triggers.create(Trigger(
            id="tr-1", slug="tr-x", name="x", description=None,
            config=WebhookTriggerConfig(), enabled=True, created_at=_now(),
        ))
        await subs.create(Subscription(
            owner=_PLATFORM, id="sb-1", trigger_id="tr-1",
            config=AgentFreshSubConfig(
                workspace_id=seeded_workspace.id, agent_id=seeded_agent.id,
            ),
            parallelism="skip", enabled=True, created_at=_now(),
        ))
    return _seed


@pytest.mark.asyncio
async def test_a_second_process_firing_the_same_skip_sub_skips_on_the_claim(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent, monkeypatch,
):
    """Process B fires while process A holds the claim: B skips at once
    (no wait for A's in-flight fire) and no second session is started."""
    from primer.trigger.skip_claim import acquire_skip_claim  # noqa: F401 (module under test)

    _seed = _seeded_skip_trigger_and_sub(
        fake_storage_provider, seeded_workspace, seeded_agent,
    )
    await _seed()

    gate = asyncio.Event()
    calls = {"n": 0}

    class _GateDispatcher:
        """Stands in for the fresh-session dispatcher: records the call, then
        sits on a gate so the fire (and its claim) stay held."""

        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            calls["n"] += 1
            await gate.wait()
            return SubscriptionDispatchResult(ok=True, artefact_id="sess-fake")

    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: _GateDispatcher())

    def _deps():
        return DispatchDeps(
            storage_provider=fake_storage_provider,
            claim_engine=fake_claim_engine,
            scheduler=fake_scheduler,
            workspace_registry=fake_workspace_registry,
        )

    fire_a = asyncio.create_task(fire_trigger(
        trigger_id="tr-1", scheduled_for=None, deps=_deps(),
    ))
    try:
        await _wait_until(lambda: calls["n"] == 1, what="process A to reach dispatch")
        # A now holds its claim inside its dispatch. Release it a moment
        # later, in the background: on the old (lock-only) code process B
        # queues behind A's lock and dispatches a second session once A
        # finishes. It is deliberately NOT cancelled after B returns: it is
        # what lets A (and, on the old code, the queued B) finish.
        async def _release_soon():
            await asyncio.sleep(0.1)
            gate.set()

        release = asyncio.create_task(_release_soon())
        try:
            res_b = await asyncio.wait_for(
                fire_trigger(trigger_id="tr-1", scheduled_for=None, deps=_deps()),
                timeout=2.0,
            )
        except TimeoutError:
            pytest.fail(
                "process B never returned: it queued behind process A's "
                "lock instead of skipping on the claim conflict"
            )
        assert res_b.results[0]["skipped"] is True, (
            "process B passed the skip check beside process A: the claim "
            "conflict must refuse it before the busy check runs"
        )
        assert res_b.results[0]["error_code"] == "skipped_subscription_claimed"
        gate.set()
        await fire_a
        await release
    finally:
        gate.set()
        if not fire_a.done():
            await fire_a
    assert calls["n"] == 1, "the claim loser dispatched a second session"


@pytest.mark.asyncio
async def test_an_expired_claim_is_taken_over_and_a_fresh_one_refuses(
    fake_storage_provider,
):
    """A holder that crashed leaves its row: after the TTL the next claim
    takes it over; a live (unexpired) holder's claim is refused."""
    from primer.model.trigger_skip_claim import TriggerSkipClaim
    from primer.trigger.skip_claim import (
        _claim_id,
        acquire_skip_claim,
        release_skip_claim,
    )

    storage = fake_storage_provider.get_storage(TriggerSkipClaim)
    now = datetime.now(timezone.utc)
    await storage.create(TriggerSkipClaim(
        id=_claim_id("sb-1"), subscription_id="sb-1",
        claimed_at=now - timedelta(minutes=5),
        expires_at=now - timedelta(minutes=4),
    ))

    generation = await acquire_skip_claim(fake_storage_provider, "sb-1")
    assert generation is not None, "after the TTL the next claim takes the stale row over"
    row = await storage.get(_claim_id("sb-1"))
    assert row is not None and row.expires_at > now, "the takeover must refresh the expiry"

    assert await acquire_skip_claim(fake_storage_provider, "sb-1") is None, (
        "a live holder's claim must be refused, not stolen"
    )

    await release_skip_claim(fake_storage_provider, "sb-1", generation)
    assert await storage.get(_claim_id("sb-1")) is None


@pytest.mark.asyncio
async def test_two_takers_of_one_expired_row_cannot_both_win(
    fake_storage_provider, monkeypatch,
):
    """F1: two fires that read the SAME expired row (interleaved at the read)
    must not both take it over: the takeover is one guarded write on the exact
    stale value, so at most one taker's guard can match."""
    from primer.model.trigger_skip_claim import TriggerSkipClaim
    from primer.trigger.skip_claim import _claim_id, acquire_skip_claim

    storage = fake_storage_provider.get_storage(TriggerSkipClaim)
    sub_id = "sb-take-1"
    now = datetime.now(timezone.utc)
    stale = await storage.create(TriggerSkipClaim(
        id=_claim_id(sub_id), subscription_id=sub_id,
        claimed_at=now - timedelta(minutes=5),
        expires_at=now - timedelta(minutes=4),
    ))

    # Interleave at the read: both takers see the SAME expired row, before
    # either of their writes lands.
    real_get = storage.get

    async def _stale_get(id, *, conn=None):
        if id == _claim_id(sub_id):
            return stale
        return await real_get(id, conn=conn)

    monkeypatch.setattr(storage, "get", _stale_get)

    a, b = await asyncio.gather(
        acquire_skip_claim(fake_storage_provider, sub_id),
        acquire_skip_claim(fake_storage_provider, sub_id),
    )
    assert (a is not None) != (b is not None), (
        "two takers of one expired claim both won: the takeover must be one "
        "guarded write on the exact stale claimed_at, not a delete + re-insert"
    )
    # Read via the UNpatched get: the monkeypatch still returns the stale
    # snapshot, which would make the winner's fresh row look expired.
    row = await real_get(_claim_id(sub_id))
    assert row is not None and row.expires_at > now, (
        "the winner's takeover must leave a fresh, unexpired row"
    )


@pytest.mark.asyncio
async def test_a_stale_holder_releasing_cannot_delete_the_takers_claim(
    fake_storage_provider,
):
    """F2: a holder whose dispatch outlived the TTL releases late: its release
    must not remove the claim a taker now holds (release is guarded on the
    holder's own generation)."""
    from primer.model.trigger_skip_claim import TriggerSkipClaim
    from primer.storage import raw_generation
    from primer.trigger.skip_claim import (
        _claim_id,
        acquire_skip_claim,
        release_skip_claim,
    )

    storage = fake_storage_provider.get_storage(TriggerSkipClaim)
    sub_id = "sb-take-2"
    now = datetime.now(timezone.utc)
    stale = await storage.create(TriggerSkipClaim(
        id=_claim_id(sub_id), subscription_id=sub_id,
        claimed_at=now - timedelta(minutes=5),
        expires_at=now - timedelta(minutes=4),
    ))
    stale_generation = raw_generation(stale, "claimed_at")

    taker_generation = await acquire_skip_claim(fake_storage_provider, sub_id)
    assert taker_generation is not None, "the taker must take the stale row over"

    # The stale holder's dispatch finally finishes and releases, with ITS stamp.
    try:
        await release_skip_claim(fake_storage_provider, sub_id, stale_generation)
    except TypeError:
        await release_skip_claim(fake_storage_provider, sub_id)  # pre-F2: unconditional

    row = await storage.get(_claim_id(sub_id))
    assert row is not None, (
        "a stale holder's release deleted the taker's claim: the release must "
        "be a delete_if on the holder's own generation"
    )
    assert raw_generation(row, "claimed_at") == taker_generation, (
        "the row must still carry the taker's stamp"
    )

    # The taker's own release removes the row.
    try:
        await release_skip_claim(fake_storage_provider, sub_id, taker_generation)
    except TypeError:
        await release_skip_claim(fake_storage_provider, sub_id)
    assert await storage.get(_claim_id(sub_id)) is None


@pytest.mark.asyncio
async def test_two_processes_over_one_sqlite_file_cannot_both_hold_the_claim(tmp_path):
    """Two real storage connections (two "processes"): the conditional
    insert lets exactly one claim the subscription."""
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider
    from primer.trigger.skip_claim import acquire_skip_claim, release_skip_claim

    path = str(tmp_path / "claims.sqlite")
    sp1 = SqliteStorageProvider(SqliteConfig(path=path))
    sp2 = SqliteStorageProvider(SqliteConfig(path=path))
    await sp1.initialize()
    await sp2.initialize()
    try:
        a, b = await asyncio.gather(
            acquire_skip_claim(sp1, "sb-9"), acquire_skip_claim(sp2, "sb-9"),
        )
        assert (a is None) != (b is None), (
            "exactly one of two concurrent claims may win the conditional insert"
        )
        for gen in (a, b):
            if gen is not None:
                await release_skip_claim(sp1, "sb-9", gen)
        assert await acquire_skip_claim(sp1, "sb-9") is not None
    finally:
        await sp1.aclose()
        await sp2.aclose()


# ---------------------------------------------------------------------------
# X2.1 round 2 (M2): the claim acquire ran OUTSIDE the per-subscription
# isolation, so a transient store error on the claim aborted the whole fire
# (the remaining subscriptions never dispatched, and the trigger row never
# got its last_fired_id marker). A claim error now fails only that
# subscription's dispatch, like any other per-sub failure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_store_error_fails_only_that_subscription(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent, monkeypatch,
):
    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)
    await triggers.create(Trigger(
        id="tr-m2", slug="tr-m2", name="m2", description=None,
        config=WebhookTriggerConfig(), enabled=True, created_at=_now(),
    ))
    for sub_id in ("sb-m2-1", "sb-m2-2"):
        await subs.create(Subscription(
            owner=_PLATFORM, id=sub_id, trigger_id="tr-m2",
            config=AgentFreshSubConfig(
                workspace_id=seeded_workspace.id, agent_id=seeded_agent.id,
            ),
            parallelism="skip", enabled=True, created_at=_now(),
        ))

    calls = {"n": 0}

    class _CountingDispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            calls["n"] += 1
            return SubscriptionDispatchResult(ok=True, artefact_id=f"sess-{sub.id}")

    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: _CountingDispatcher())

    real_acquire = dispatch_mod.acquire_skip_claim

    async def _failing_acquire(sp, sub_id):
        if sub_id == "sb-m2-1":
            raise RuntimeError("transient store failure on the claim")
        return await real_acquire(sp, sub_id)

    monkeypatch.setattr(dispatch_mod, "acquire_skip_claim", _failing_acquire)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    res = await fire_trigger(trigger_id="tr-m2", scheduled_for=None, deps=deps)

    by_sub = {r["subscription_id"]: r for r in res.results}
    assert by_sub["sb-m2-1"]["ok"] is False
    assert by_sub["sb-m2-1"]["error_code"] == "skip_claim_failed"
    assert by_sub["sb-m2-2"]["ok"] is True, (
        "a claim error on one subscription aborted the whole fire: the "
        "sibling never dispatched"
    )
    assert calls["n"] == 1, "the claim-failing subscription must not dispatch"
    trigger = await triggers.get("tr-m2")
    assert trigger.last_fired_id is not None, (
        "the aborted fire never recorded its marker: a redelivery would "
        "refire the sibling the first pass already served"
    )
