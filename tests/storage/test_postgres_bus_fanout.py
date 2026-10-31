"""The Postgres bus over a real LISTEN/NOTIFY (perf plan Task 6): one shared listener serves every subscription. Live Postgres only."""
from __future__ import annotations

import asyncio

import pytest

from primer.bus.postgres import PostgresEventBus
from tests.pg_gate import needs_postgres

pg = needs_postgres("Postgres bus fan-out tests")


async def _take(sub, n, timeout=5.0):
    return [await asyncio.wait_for(sub.__anext__(), timeout) for _ in range(n)]


@pg
@pytest.mark.asyncio
async def test_many_subscriptions_share_one_listener_and_each_gets_what_it_asked_for(postgres_storage_provider):
    listener_bus = PostgresEventBus(postgres_storage_provider)
    publisher_bus = PostgresEventBus(postgres_storage_provider)
    firehose = [listener_bus.subscribe() for _ in range(20)]
    cancel = listener_bus.subscribe(key="session:perf-t6:cancel", coalesce=True)
    prefixed = listener_bus.subscribe(prefix="perf-t6-inv:")
    await firehose[0].ready()
    assert listener_bus._listener is not None and len(listener_bus._subs) == 22  # one supervised connection behind all of them

    for key in ("perf-t6-inv:a", "session:perf-t6:tick", "session:perf-t6:cancel", "perf-t6-inv:b"):
        await publisher_bus.publish(key, {"k": key})

    assert [e.event_key for e in await _take(cancel, 1)] == ["session:perf-t6:cancel"]
    assert [e.event_key for e in await _take(prefixed, 2)] == ["perf-t6-inv:a", "perf-t6-inv:b"]
    for sub in firehose:
        assert len(await _take(sub, 4)) == 4

    for sub in (*firehose, cancel, prefixed):
        await sub.aclose()
    assert listener_bus._listener is None  # the connection went back to the pool with the last subscription
    await listener_bus.aclose()
    await publisher_bus.aclose()
