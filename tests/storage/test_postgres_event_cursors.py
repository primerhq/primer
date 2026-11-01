"""The Postgres event store's batched cursor read and write (perf plan Task 1): one statement each, same results as the single-row methods."""
from __future__ import annotations

import pytest

from tests.pg_gate import needs_postgres

pg = needs_postgres("Postgres event cursor tests")


@pg
@pytest.mark.asyncio
async def test_batched_cursors_round_trip_on_postgres(postgres_storage_provider):
    store = postgres_storage_provider.get_event_store()
    await store.ensure_schema()
    assert await store.get_cursors([]) == {}
    await store.set_cursors({"a": 3, "b": 7})
    await store.set_cursors({"b": 9, "c": 1})
    assert await store.get_cursors(["a", "b", "c", "missing"]) == {"a": 3, "b": 9, "c": 1}
    assert await store.get_cursor("b") == 9
    for sub in ("a", "b", "c"):
        await store.delete_cursor(sub)
