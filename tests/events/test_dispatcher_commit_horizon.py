"""The dispatcher never moves a cursor past an event id that may still commit (core review finding 2).

Postgres ids come from a sequence at INSERT, not at COMMIT: a transaction that inserted event N and commits after N+1 left a hole the cursor had
already passed, so event N was never delivered. A gap below the next event now holds the cursor until the transactions that were in flight when the
gap was first seen have finished (the store's ``read_horizon``: the oldest and the next transaction id), and a short grace has passed. A gap left by
a rollback is permanent and releases the same way. A store with no horizon (SQLite: one writer; the in-memory store) behaves as before.
"""
from __future__ import annotations

from datetime import UTC, datetime

from primer.events.dispatcher import HOLE_GRACE_SECONDS, EventDispatcher
from primer.events.memory_store import InMemoryEventStore
from primer.model.event import Event, EventSubscription, LogSink
from tests.conftest import _FakeStorageProvider


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _HorizonStore(InMemoryEventStore):
    """A log whose ids can be committed out of order, and whose transaction horizon the test moves."""

    def __init__(self) -> None:
        super().__init__()
        self.xmin, self.xmax = 100, 100
        self.horizon_reads = 0

    def commit(self, event_id: int) -> None:
        self._events.append(Event(id=event_id, event_type="t.x", occurred_at=datetime.now(UTC), payload={}))
        self._events.sort(key=lambda e: e.id)
        self._next_id = max(self._next_id, event_id + 1)

    async def read_horizon(self):
        self.horizon_reads += 1
        return self.xmin, self.xmax


class _Delivered(EventDispatcher):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.ids: list[int] = []

    async def _deliver(self, sub, event):  # type: ignore[override]
        self.ids.append(event.id)
        return True


async def _world(store):
    sp = _FakeStorageProvider()
    sp._event_store = store
    await sp.get_storage(EventSubscription).create(EventSubscription(id="s1", description="t", sink=LogSink()))
    clock = _Clock()
    d = _Delivered(storage_provider=sp, clock=clock)
    await d.drain_once()  # seeds the cursor at head (0)
    return sp, d, clock


async def test_a_store_without_a_horizon_passes_a_gap_as_before():
    store = InMemoryEventStore()
    sp, d, _ = await _world(store)
    await store.append(event_type="t.x")
    store._next_id = 5  # ids 2-4 never exist
    await store.append(event_type="t.x")
    await d.drain_once()
    assert d.ids == [1, 5]


async def test_an_event_behind_a_hole_waits_and_the_hole_is_delivered_first_when_it_commits():
    store = _HorizonStore()
    sp, d, clock = await _world(store)
    store.commit(1)
    store.commit(3)  # 2 is still in flight
    store.xmin, store.xmax = 100, 105  # the transaction that owns it (xid 100) is running
    await d.drain_once()
    assert d.ids == [1]  # 3 is held behind the hole
    clock.now += 30  # past the grace, under the ceiling (HOLE_MAX_WAIT_SECONDS)
    await d.drain_once()  # the horizon has not moved: the transaction that may own 2 is still running
    assert d.ids == [1]
    store.commit(2)  # it commits at last
    await d.drain_once()
    assert d.ids == [1, 2, 3] and await store.get_cursor("s1") == 3


async def test_a_permanent_hole_releases_once_the_transactions_in_flight_have_finished_and_the_grace_passed():
    store = _HorizonStore()
    sp, d, clock = await _world(store)
    store.commit(1)
    store.commit(3)  # 2 was rolled back: it never appears
    store.xmin, store.xmax = 100, 105  # a transaction (xid 100) is running; the next xid is 105
    await d.drain_once()
    assert d.ids == [1]
    clock.now += HOLE_GRACE_SECONDS + 1
    await d.drain_once()
    assert d.ids == [1]  # the grace passed, but xid 100 may still own the hole
    store.xmin = 105  # every transaction that was running when the hole was seen has finished
    await d.drain_once()
    assert d.ids == [1, 3]


async def test_the_grace_must_pass_even_when_no_transaction_is_running():
    store = _HorizonStore()
    sp, d, clock = await _world(store)
    store.commit(1)
    store.commit(3)
    store.xmin, store.xmax = 105, 105  # nothing in flight
    await d.drain_once()
    assert d.ids == [1]  # first sighting: held
    await d.drain_once()
    assert d.ids == [1]  # no time has passed
    clock.now += HOLE_GRACE_SECONDS + 0.1
    await d.drain_once()
    assert d.ids == [1, 3]


async def test_contiguous_ids_never_consult_the_horizon():
    store = _HorizonStore()
    sp, d, _ = await _world(store)
    for i in (1, 2, 3):
        store.commit(i)
    await d.drain_once()
    assert d.ids == [1, 2, 3] and store.horizon_reads == 0


async def test_a_horizon_that_never_clears_cannot_hold_delivery_back_for_ever(caplog):
    """Transaction ids are cluster-wide: a long transaction anywhere keeps the oldest running id back. The gap is passed at the ceiling, with one warning."""
    import logging

    from primer.events.dispatcher import HOLE_MAX_WAIT_SECONDS

    store = _HorizonStore()
    sp, d, clock = await _world(store)
    await sp.get_storage(EventSubscription).create(EventSubscription(id="s2", description="t", sink=LogSink()))
    await d.drain_once()  # seeds the second subscription at head too
    store.commit(1)
    store.commit(3)
    store.xmin, store.xmax = 100, 105  # one transaction (xid 100) never finishes
    await d.drain_once()  # first sight of the gap
    assert d.ids == [1, 1]  # event 1 reached both subscriptions; 3 waits behind the gap
    clock.now += HOLE_MAX_WAIT_SECONDS - 1
    with caplog.at_level(logging.WARNING, logger="primer.events.dispatcher"):
        await d.drain_once()
        assert d.ids == [1, 1], "still held at 59s"
        assert not [r for r in caplog.records if "passing event ids" in r.getMessage()]
        clock.now += 1
        await d.drain_once()
    assert sorted(d.ids) == [1, 1, 3, 3]  # both subscriptions got 1 and 3
    warnings = [r for r in caplog.records if "passing event ids" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "2-2" in warnings[0].getMessage() and "60" in warnings[0].getMessage()
