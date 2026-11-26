"""6.3: a wake that arrives before its park is durable survives the race.

The tools create the one-shot subscription before the park row is durable.
The dispatcher used to count every "not parked yet" pass as a sink failure;
after ``max_failures`` it skipped the event AND deleted the subscription, so
the park that landed later had no subscriber left to deliver the wake into.
Pending is a retry, not a failure: the one-shot GC stays with the states
that mean the wait is over (the session ended or vanished, the park names
another entry, the park moved on to other keys).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import AsyncIterator

import pytest_asyncio

from primer.events.dispatcher import EventDispatcher
from primer.model.event import EventFilter, EventSubscription, SessionWakeSink
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage.sqlite import SqliteStorageProvider
from tests.events._users import seed_account_of
from tests.events.test_dispatcher import ADMIN_REF, _RecordingBus, _sub


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _dispatcher(sp, **kwargs) -> EventDispatcher:
    return EventDispatcher(storage_provider=sp, event_bus=_RecordingBus(), **kwargs)


async def _alive_session(sp, session_id: str) -> None:
    """A session whose turn is in flight: the park write has not landed yet."""
    await seed_account_of(sp, ADMIN_REF)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=session_id,
        workspace_id="w",
        binding=AgentSessionBinding(agent_id="agent-a"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        initiated_by=ADMIN_REF,
        turn_status="running",
    ))


async def _land_park(sp, session_id: str, event_key: str) -> None:
    """The park write lands (the worker settled the turn)."""
    storage = sp.get_storage(WorkspaceSession)
    row = await storage.get(session_id)
    await storage.update(row.model_copy(update={
        "parked_status": "parked",
        "parked_event_key": event_key,
        "parked_at": datetime.now(timezone.utc),
        "parked_state": {
            "schema_version": 1,
            "yielded": {
                "tool_name": "wait_for_event",
                "event_key": event_key,
                "resume_metadata": {},
            },
        },
    }))


async def test_a_wake_before_its_park_survives_the_race(sp) -> None:
    store = sp.get_event_store()
    sid, key = "s-race", "evwait:s-race:tc-1"
    await _alive_session(sp, sid)
    await _sub(sp, sink=SessionWakeSink(event_key=key, session_id=sid, one_shot=True))
    d = _dispatcher(sp, max_failures=2)
    await d.drain_once()  # pins the fresh subscription's cursor at head

    event_id = await store.append(event_type="session.steered")
    # Passes BEFORE the park lands: old code counted each as a failure and
    # deleted the one-shot after max_failures; the wake then had no
    # subscriber when the park did land.
    for _ in range(4):
        assert await d.drain_once() == 0
        assert await sp.get_storage(EventSubscription).get("sub-1") is not None, (
            "a not-yet-durable park is a retry, not a failure: the "
            "subscription must not be collected"
        )

    await _land_park(sp, sid, key)
    assert await d.drain_once() == 1  # the park landed: delivered immediately
    row = await sp.get_storage(WorkspaceSession).get(sid)
    assert row.parked_status == "resumable"
    assert await sp.get_storage(EventSubscription).get("sub-1") is None, (
        "the one-shot is consumed on delivery"
    )


async def test_a_park_under_other_keys_collects_the_subscription_immediately(sp) -> None:
    """The session is parked, but under keys this subscription never waited
    on (its wait timed out and the session re-parked elsewhere): the event
    is consumed and the one-shot is collected on the first pass, not after
    max_failures of retries."""
    store = sp.get_event_store()
    sid, key = "s-moved", "evwait:s-moved:tc-1"
    await _alive_session(sp, sid)
    await _sub(sp, sink=SessionWakeSink(event_key=key, session_id=sid, one_shot=True))
    d = _dispatcher(sp, max_failures=99)
    await d.drain_once()
    await store.append(event_type="session.steered")

    await _land_park(sp, sid, "ask_user:s-moved:other")
    assert await d.drain_once() == 1
    assert await sp.get_storage(EventSubscription).get("sub-1") is None
    row = await sp.get_storage(WorkspaceSession).get(sid)
    assert row.parked_status == "parked", "the later park is left alone"


async def test_an_ended_session_collects_the_subscription_immediately(sp) -> None:
    store = sp.get_event_store()
    sid, key = "s-ended", "evwait:s-ended:tc-1"
    await _alive_session(sp, sid)
    storage = sp.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    await storage.update(row.model_copy(update={"status": SessionStatus.ENDED}))
    await _sub(sp, sink=SessionWakeSink(event_key=key, session_id=sid, one_shot=True))
    d = _dispatcher(sp, max_failures=99)
    await d.drain_once()
    await store.append(event_type="session.steered")

    assert await d.drain_once() == 1
    assert await sp.get_storage(EventSubscription).get("sub-1") is None


async def test_a_wait_made_during_a_resumed_turn_survives_the_first_matching_event(sp) -> None:
    """The row keeps the OLD park (``resumable``, the old keys) until the
    resumed turn releases. A wait the turn makes meanwhile (a graph
    superstep after a gate) is live: its first matching event must not be
    consumed and its subscription deleted as 'parked under other keys'."""
    store = sp.get_event_store()
    sid, new_key = "s-resumed", "evwait:s-resumed:tc-2"
    await _alive_session(sp, sid)
    storage = sp.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    await storage.update(row.model_copy(update={
        "parked_status": "resumable",  # the old park, being resumed
        "parked_event_key": "ask_user:s-resumed:tc-1",
        "parked_at": datetime.now(timezone.utc),
        "turn_status": "running",
    }))
    await _sub(sp, sink=SessionWakeSink(event_key=new_key, session_id=sid, one_shot=True))
    d = _dispatcher(sp, max_failures=1)  # one failure would collect it
    await d.drain_once()
    await store.append(event_type="session.steered")

    for _ in range(3):
        assert await d.drain_once() == 0
        assert await sp.get_storage(EventSubscription).get("sub-1") is not None, (
            "a resumable row with a running turn holds the subscription"
        )

    # The turn releases and the new park lands: the held event wakes it.
    await _land_park(sp, sid, new_key)
    assert await d.drain_once() == 1
    assert (await storage.get(sid)).parked_status == "resumable"
    assert await sp.get_storage(EventSubscription).get("sub-1") is None


async def test_an_idle_unparked_session_collects_within_max_failures(sp) -> None:
    """A Stop during a machine wait, a reset that cleared the park: the
    session is idle and unparked, no park will ever land. It counts failures
    like any sink failure and the one-shot is collected, not held forever."""
    store = sp.get_event_store()
    sid, key = "s-idle", "evwait:s-idle:tc-1"
    await _alive_session(sp, sid)
    storage = sp.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    await storage.update(row.model_copy(update={"turn_status": "idle"}))
    await _sub(sp, sink=SessionWakeSink(event_key=key, session_id=sid, one_shot=True))
    d = _dispatcher(sp, max_failures=3)
    await d.drain_once()
    await store.append(event_type="session.steered")

    for _ in range(2):
        await d.drain_once()
        assert await sp.get_storage(EventSubscription).get("sub-1") is not None
    await d.drain_once()  # the third failure
    assert await sp.get_storage(EventSubscription).get("sub-1") is None


async def test_a_resumable_row_awaiting_its_resume_holds_without_counting_failures(sp) -> None:
    """The row is 'resumable' (its park resolved, the resumed turn not yet
    claimed) and the event's key is not one the row parks on: the resumed turn
    may still make a wait under this key before its release, so the pass is
    pending, NOT a failure - it used to fall to the failure counter and the
    one-shot could be collected by max_failures while the resume was still
    queued for a worker (review 2026-11-23, I1)."""
    store = sp.get_event_store()
    sid, new_key = "s-await-resume", "evwait:s-await-resume:tc-2"
    await _alive_session(sp, sid)
    storage = sp.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    await storage.update(row.model_copy(update={
        "parked_status": "resumable",  # the old park, being resumed
        "parked_event_key": "ask_user:s-await-resume:tc-1",
        "parked_at": datetime.now(timezone.utc),
        "turn_status": "idle",  # the resumed turn has not been claimed yet
    }))
    await _sub(sp, sink=SessionWakeSink(event_key=new_key, session_id=sid, one_shot=True))
    d = _dispatcher(sp, max_failures=2)  # the old fall-through collected it after two passes
    await d.drain_once()
    await store.append(event_type="session.steered")

    for _ in range(4):
        assert await d.drain_once() == 0
        assert await sp.get_storage(EventSubscription).get("sub-1") is not None, (
            "a RUNNING row whose resolved park awaits its resumed turn is pending, not a failure"
        )

    # The resumed turn releases and the new park lands under this key: the held
    # event wakes it.
    await _land_park(sp, sid, new_key)
    assert await d.drain_once() == 1
    assert (await storage.get(sid)).parked_status == "resumable"
    assert await sp.get_storage(EventSubscription).get("sub-1") is None


async def test_a_paused_resumable_row_still_counts_failures(sp) -> None:
    """A pause can land on a 'resumable' row between the flip and the claim;
    the row may sit paused indefinitely, so it must not hold the cursor
    forever: it falls to the failure count and the one-shot is collected
    (review 2026-11-23, I1)."""
    store = sp.get_event_store()
    sid, key = "s-paused", "evwait:s-paused:tc-2"
    await _alive_session(sp, sid)
    storage = sp.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    await storage.update(row.model_copy(update={
        "status": SessionStatus.PAUSED,
        "parked_status": "resumable",
        "parked_event_key": "ask_user:s-paused:tc-1",
        "parked_at": datetime.now(timezone.utc),
        "turn_status": "idle",
    }))
    await _sub(sp, sink=SessionWakeSink(event_key=key, session_id=sid, one_shot=True))
    d = _dispatcher(sp, max_failures=3)
    await d.drain_once()
    await store.append(event_type="session.steered")

    for _ in range(2):
        await d.drain_once()
        assert await sp.get_storage(EventSubscription).get("sub-1") is not None
    await d.drain_once()  # the third failure
    assert await sp.get_storage(EventSubscription).get("sub-1") is None
