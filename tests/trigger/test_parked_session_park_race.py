"""6.3: a trigger fire that races its park write keeps the subscription.

The parked_session dispatcher used to delete the subscription on the FIRST
fire that saw ``parked_status != "parked"`` - which is exactly what a
session looks like while its turn is still writing the park (the tool
created the subscription before the park row was durable). An ABANDONED
park (the turn ended, the park never came) still deletes itself (pinned in
test_parked_session_fencing.py); a turn-in-flight park must not: the
subscription stays so a later fire (a recurring tick, a redelivery) finds
the landed park and wakes the session.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from primer.bus.in_memory import InMemoryEventBus
from primer.model.principal import PrincipalRef
from primer.model.trigger import Subscription
from primer.model.workspace_session import WorkspaceSession
from primer.trigger.subscribers import DispatchDeps
from primer.trigger.subscribers.parked_session import ParkedSessionDispatcher
from tests.conftest import _FakeStorageProvider
from tests.trigger.test_parked_session_fencing import _fire, _seed, _sub


async def test_a_fire_racing_the_park_keeps_the_subscription() -> None:
    """The turn is in flight and the park write has not landed: the fire
    skips WITHOUT deleting - a later fire can still wake the session."""
    sp = _FakeStorageProvider()
    await _seed(sp, parked_status=None, parked_state=None, turn_status="running")
    result, row = await _fire(sp)
    assert result.ok is True
    assert result.skipped is True
    assert result.error_code == "skipped_park_pending"
    assert row is not None, (
        "the park is still being written; deleting now loses the wake "
        "for a one-shot fire"
    )


async def test_the_park_lands_then_a_later_fire_wakes_the_session() -> None:
    """First fire races the park (skipped, row kept); the park lands; the
    next fire finds it and publishes the wake onto the park's key."""
    sp = _FakeStorageProvider()
    await _seed(sp, parked_status=None, parked_state=None, turn_status="running")
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        sub = _sub()
        # A system owner reaches every workspace (the reach check on the
        # second fire); the race fire (first) never reaches that check.
        sub = sub.model_copy(update={"owner": PrincipalRef.system()})
        await sp.get_storage(Subscription).create(sub)
        deps = DispatchDeps(
            storage_provider=sp, claim_engine=None, event_bus=bus,
        )
        d = ParkedSessionDispatcher()

        r1 = await d.dispatch(
            sub, rendered_payload="", fire_context={"fire_id": "f1"},
            fire_id="f1", deps=deps,
        )
        assert r1.error_code == "skipped_park_pending"

        # The park write lands: the entry carries THIS subscription's id
        # (the tool stamped the park it created).
        storage = sp.get_storage(WorkspaceSession)
        row = await storage.get("s-park")
        key = "trigger:s-park:tr-park"
        await storage.update(row.model_copy(update={
            "parked_status": "parked",
            "parked_event_key": key,
            "parked_at": datetime.now(UTC),
            "parked_state": {
                "tool_call_id": "tc-1",
                "binding_epoch": 0,
                "yielded": {
                    "tool_name": "subscribe_to_trigger",
                    "event_key": key,
                    "resume_metadata": {"subscription_id": "sb-park"},
                },
            },
        }))

        still_there = await sp.get_storage(Subscription).get("sb-park")
        assert still_there is not None
        sub2 = still_there
        sub_view = bus.subscribe()  # before the fire: the bus is broadcast, no replay
        try:
            r2 = await d.dispatch(
                sub2, rendered_payload="", fire_context={"fire_id": "f2"},
                fire_id="f2", deps=deps,
            )
            assert r2.ok is True
            assert r2.skipped is False
            assert r2.artefact_id == "s-park"

            # The wake was published onto the park's key.
            event = await asyncio.wait_for(anext(sub_view), timeout=1.0)
            assert event.event_key == key
        finally:
            await sub_view.aclose()

        # The one-shot subscription is consumed.
        assert await sp.get_storage(Subscription).get("sb-park") is None
    finally:
        await bus.aclose()


async def test_an_abandoned_park_still_self_deletes() -> None:
    """The turn is NOT in flight and the park never came (the abandon
    chokepoint cleared it): the old behaviour stands."""
    sp = _FakeStorageProvider()
    await _seed(sp, parked_status=None, parked_state=None)  # turn_status "idle"
    result, row = await _fire(sp)
    assert result.ok is True
    assert result.skipped is True
    assert result.error_code == "skipped_session_unparked"
    assert row is None


async def test_a_fire_during_a_resumed_turn_keeps_the_subscription() -> None:
    """The row keeps its OLD park (``resumable``) until the resumed turn
    releases; a subscription the turn made meanwhile (a graph superstep
    after a gate) is live, so the fire skips WITHOUT deleting it."""
    sp = _FakeStorageProvider()
    await _seed(
        sp, parked_status="resumable", turn_status="running",
        parked_state={"tool_call_id": "tc-old", "binding_epoch": 0},
    )
    result, row = await _fire(sp)
    assert result.skipped is True
    assert result.error_code == "skipped_park_pending"
    assert row is not None


async def test_a_session_that_is_idle_and_unparked_still_self_deletes() -> None:
    """Neither parked, resumable nor running: the turn ended and the park
    never came, so the subscription is an orphan and collects."""
    sp = _FakeStorageProvider()
    await _seed(sp, parked_status=None, parked_state=None, turn_status="idle")
    result, row = await _fire(sp)
    assert result.error_code == "skipped_session_unparked"
    assert row is None
