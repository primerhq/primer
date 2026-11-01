"""Postgres: an event committed out of id order is delivered, not skipped (core review finding 2). Runs only against a live database.

Event ids come from a sequence taken at INSERT. Transaction A inserts event N and stays open; transaction B inserts N+1 and commits; a dispatcher
tick sees only N+1. The cursor must not pass N: when A commits, N is delivered before N+1.
"""
from __future__ import annotations

import pytest

from primer.events.dispatcher import HOLE_GRACE_SECONDS, EventDispatcher
from primer.model.event import EventSubscription, LogSink
from tests.pg_gate import needs_postgres

pg = needs_postgres("Postgres event commit order tests")


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Recording(EventDispatcher):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.ids: list[int] = []

    async def _deliver(self, sub, event):  # type: ignore[override]
        self.ids.append(event.id)
        return True


async def _world(sp, sub_id: str):
    store = sp.get_event_store()
    await store.ensure_schema()
    await sp.get_storage(EventSubscription).create(EventSubscription(id=sub_id, description="t", sink=LogSink()))
    clock = _Clock()
    d = _Recording(storage_provider=sp, clock=clock)
    await d.drain_once()  # seeds the cursor at the current head
    return store, d, clock


async def _cleanup(sp, store, sub_id: str) -> None:
    await sp.get_storage(EventSubscription).delete(sub_id)
    await store.delete_cursor(sub_id)


@pg
@pytest.mark.asyncio
async def test_an_event_that_commits_after_a_later_one_is_still_delivered_first(postgres_storage_provider):
    sp = postgres_storage_provider
    store, d, clock = await _world(sp, "sub-commit-order")
    conn_a = await sp.pool.acquire()
    tx = conn_a.transaction()
    await tx.start()
    try:
        first = await store.append(event_type="t.first", conn=conn_a)  # id taken now, committed later
        second = await store.append(event_type="t.second")  # a later id, committed first
        assert second > first
        await d.drain_once()
        clock.now += HOLE_GRACE_SECONDS + 1
        await d.drain_once()
        assert d.ids == [], "the later event must wait while the earlier id may still commit"
    finally:
        await tx.commit()
        await sp.pool.release(conn_a)
    await d.drain_once()
    assert d.ids == [first, second]
    await _cleanup(sp, store, "sub-commit-order")


@pg
@pytest.mark.asyncio
async def test_a_rolled_back_insert_leaves_a_hole_that_releases(postgres_storage_provider):
    sp = postgres_storage_provider
    store, d, clock = await _world(sp, "sub-rollback-hole")
    conn_a = await sp.pool.acquire()
    tx = conn_a.transaction()
    await tx.start()
    try:
        lost = await store.append(event_type="t.lost", conn=conn_a)
        kept = await store.append(event_type="t.kept")
        assert kept > lost
    finally:
        await tx.rollback()
        await sp.pool.release(conn_a)
    await d.drain_once()  # first sight of the hole
    clock.now += HOLE_GRACE_SECONDS + 1
    await d.drain_once()
    assert d.ids == [kept]
    await _cleanup(sp, store, "sub-rollback-hole")
