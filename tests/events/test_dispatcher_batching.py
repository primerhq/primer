"""The event dispatcher delivers what it always did, to every subscription, with a bounded number of store reads (perf plan Task 1, finding H5).

The characterization tests pin today's observable behaviour (they pass on the unchanged dispatcher and stay green after the change); the
201-subscription test is the correctness bug the review found (only the first 200 subscriptions were ever processed); the perf tests assert
store reads per tick are independent of the subscription count.
"""
from __future__ import annotations

import pytest

from primer.events.dispatcher import EventDispatcher
from primer.model.event import EventFilter, EventSubscription, FieldMatcher, LogSink, SessionWakeSink
from tests.conftest import _FakeStorageProvider
from tests.perf.counting import CountingStorageProvider


class _Bus:
    def __init__(self, fail_times: int = 0) -> None:
        self.published: list[tuple[str, dict]] = []
        self._fail = fail_times

    async def publish(self, key, payload=None):
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("bus down")
        self.published.append((key, payload or {}))


class _Recording(EventDispatcher):
    """The real dispatcher, remembering what it delivered to whom, in order."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.delivered: list[tuple[str, int]] = []

    async def _deliver(self, sub, event):  # type: ignore[override]
        ok = await super()._deliver(sub, event)
        if ok:
            self.delivered.append((sub.id, event.id))
        return ok


async def _sub(sp, sub_id, *, sink=None, filter_=None, paused=False):
    row = EventSubscription(id=sub_id, description="t", filter=filter_ or EventFilter(), sink=sink or LogSink(), paused=paused)
    await sp.get_storage(EventSubscription).create(row)
    return row


async def _events(sp, n, event_type="agent.created"):
    store = sp.get_event_store()
    return [await store.append(event_type=event_type, entity_kind="agent", entity_id=f"a{i}") for i in range(n)]


@pytest.fixture
def sp():
    return _FakeStorageProvider()


# -- characterization: pass on the unchanged dispatcher -----------------------------------------------------------------------------------------


async def test_a_subscription_receives_every_event_after_its_start_in_order(sp):
    await _sub(sp, "s1")
    d = _Recording(storage_provider=sp, batch=3)
    await d.drain_once()  # pins the cursor at head
    ids = await _events(sp, 8)  # more than one batch of 3
    assert await d.drain_once() == 8
    assert [e for s, e in d.delivered] == ids
    assert await sp.get_event_store().get_cursor("s1") == ids[-1]


async def test_a_new_subscription_starts_at_head_and_replays_nothing(sp):
    await _events(sp, 4)
    await _sub(sp, "s1")
    d = _Recording(storage_provider=sp)
    assert await d.drain_once() == 0
    assert await sp.get_event_store().get_cursor("s1") == 4
    new = await _events(sp, 2)
    assert await d.drain_once() == 2 and [e for _, e in d.delivered] == new


async def test_two_subscriptions_at_different_cursors_each_get_exactly_their_events(sp):
    await _sub(sp, "early")
    d = _Recording(storage_provider=sp, batch=2)
    await d.drain_once()
    first = await _events(sp, 3)
    await _sub(sp, "late")  # created after the first events: starts at head
    await d.drain_once()
    second = await _events(sp, 3)
    await d.drain_once()
    assert [e for s, e in d.delivered if s == "early"] == first + second
    assert [e for s, e in d.delivered if s == "late"] == second


async def test_paused_subscriptions_are_skipped_and_keep_their_cursor(sp):
    await _sub(sp, "on")
    await _sub(sp, "off", paused=True)
    d = _Recording(storage_provider=sp)
    await d.drain_once()
    await _events(sp, 2)
    await d.drain_once()
    assert {s for s, _ in d.delivered} == {"on"}
    assert await sp.get_event_store().get_cursor("off") is None


async def test_filters_gate_delivery_but_the_cursor_still_moves(sp):
    await _sub(sp, "types", filter_=EventFilter(event_types=["agent.*"], exclude_types=["agent.deleted"]))
    await _sub(sp, "fields", filter_=EventFilter(fields=[FieldMatcher(path="entity_id", op="eq", value="a1")]))
    d = _Recording(storage_provider=sp)
    await d.drain_once()
    store = sp.get_event_store()
    e0 = await store.append(event_type="session.steered")
    e1 = await store.append(event_type="agent.created", entity_kind="agent", entity_id="a1")
    e2 = await store.append(event_type="agent.deleted", entity_kind="agent", entity_id="a2")
    await d.drain_once()
    assert [e for s, e in d.delivered if s == "types"] == [e1]
    assert [e for s, e in d.delivered if s == "fields"] == [e1]
    assert await store.get_cursor("types") == await store.get_cursor("fields") == e2 and e0 < e1 < e2


async def test_a_failing_sink_holds_only_its_own_cursor(sp):
    store = sp.get_event_store()
    await _sub(sp, "bad", sink=SessionWakeSink(event_key="evwait:s1:c1", session_id="s1", one_shot=False))
    await _sub(sp, "good")
    await _park(sp, "s1", "evwait:s1:c1")
    d = _Recording(storage_provider=sp, event_bus=_Bus(fail_times=99), max_failures=3)
    await d.drain_once()
    base = await store.get_cursor("bad")
    ev = await store.append(event_type="session.steered")
    await d.drain_once()
    await d.drain_once()
    assert await store.get_cursor("bad") == base  # held: two failing passes
    assert await store.get_cursor("good") == ev  # the healthy one moved on
    await d.drain_once()  # third failure: skipped
    assert await store.get_cursor("bad") == ev


async def test_a_one_shot_wake_completes_and_removes_its_subscription_and_cursor(sp):
    await _sub(sp, "w", sink=SessionWakeSink(event_key="evwait:s1:c1", session_id="s1", one_shot=True))
    await _park(sp, "s1", "evwait:s1:c1")
    bus = _Bus()
    d = _Recording(storage_provider=sp, event_bus=bus)
    await d.drain_once()
    await sp.get_event_store().append(event_type="session.steered")
    assert await d.drain_once() == 1
    assert [k for k, _ in bus.published] == ["evwait:s1:c1"]
    assert await sp.get_storage(EventSubscription).get("w") is None
    assert await sp.get_event_store().get_cursor("w") is None


async def _park(sp, session_id, event_key):
    from datetime import UTC, datetime

    from primer.model.principal import PrincipalRef
    from primer.model.user import User
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
    from tests.events._users import seed_account_of

    ref = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
    await seed_account_of(sp, ref)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=session_id, workspace_id="w", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=datetime.now(UTC), initiated_by=ref, parked_status="parked", parked_event_key=event_key,
    ))
    assert User  # imported for the account seeding above


# -- the correctness bug: only the first 200 subscriptions were ever processed ---------------------------------------------------------------


async def test_every_subscription_is_processed_not_just_the_first_200(sp):
    for i in range(250):
        await _sub(sp, f"s{i:03d}")
    d = _Recording(storage_provider=sp)
    await d.drain_once()
    ids = await _events(sp, 2)
    await d.drain_once()
    store = sp.get_event_store()
    behind = [i for i in range(250) if await store.get_cursor(f"s{i:03d}") != ids[-1]]
    assert behind == [], f"{len(behind)} subscriptions never advanced (first: s{behind[0]:03d})" if behind else ""
    assert len({s for s, _ in d.delivered}) == 250


# -- perf: store reads per tick do not grow with the number of subscriptions ----------------------------------------------------------------


async def test_store_reads_per_tick_are_independent_of_the_subscription_count():
    inner = _FakeStorageProvider()
    sp = CountingStorageProvider(inner)
    for i in range(50):
        await _sub(sp, f"s{i:02d}")
    d = _Recording(storage_provider=sp, batch=200)
    await d.drain_once()
    await _events(sp, 20)
    with sp.counted() as c:
        await d.drain_once()
    cursor_reads = c.calls("get_cursor", "get_cursors", label="events")
    event_reads = c.calls("read_after", label="events")
    cursor_writes = c.calls("set_cursor", "set_cursors", label="events")
    assert event_reads <= 3, event_reads
    assert cursor_reads <= 2, cursor_reads
    assert cursor_writes <= 2, cursor_writes
    assert len({s for s, _ in d.delivered}) == 50


async def test_an_idle_tick_reads_cursors_once_and_events_at_most_once():
    sp = CountingStorageProvider(_FakeStorageProvider())
    for i in range(50):
        await _sub(sp, f"s{i:02d}")
    d = _Recording(storage_provider=sp)
    await d.drain_once()
    with sp.counted() as c:
        await d.drain_once()
    assert c.calls("get_cursor", "get_cursors", label="events") <= 1
    assert c.calls("read_after", label="events") <= 1
    assert c.calls("set_cursor", "set_cursors", label="events") == 0
