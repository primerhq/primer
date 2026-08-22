"""Contract tests for the platform event log store (SQLite backend).

The Postgres implementation shares the interface and is exercised by
the live-server e2e lane; these tests pin the contract on the backend
the unit lanes run (the content-store precedent).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_append_assigns_increasing_ids(sp):
    store = sp.get_event_store()
    first = await store.append(event_type="agent.created",
                               entity_kind="agent", entity_id="a1",
                               payload={"id": "a1"})
    second = await store.append(event_type="agent.updated",
                                entity_kind="agent", entity_id="a1")
    assert second > first
    assert await store.max_id() == second


async def test_read_after_returns_rows_above_cursor_in_order(sp):
    store = sp.get_event_store()
    ids = [await store.append(event_type=f"kind.v{i}") for i in range(4)]
    events = await store.read_after(ids[1])
    assert [e.id for e in events] == ids[2:]
    assert [e.event_type for e in events] == ["kind.v2", "kind.v3"]
    assert await store.read_after(ids[-1]) == []


async def test_read_after_respects_limit_and_filters(sp):
    store = sp.get_event_store()
    await store.append(event_type="agent.created", entity_kind="agent",
                       entity_id="a1", workspace_id="ws1")
    await store.append(event_type="graph.created", entity_kind="graph",
                       entity_id="g1")
    await store.append(event_type="agent.deleted", entity_kind="agent",
                       entity_id="a2")

    only_agents = await store.read_after(0, event_type_prefix="agent.")
    assert [e.event_type for e in only_agents] == [
        "agent.created", "agent.deleted",
    ]
    by_entity = await store.read_after(0, entity_kind="agent",
                                       entity_id="a1")
    assert len(by_entity) == 1
    assert by_entity[0].workspace_id == "ws1"
    limited = await store.read_after(0, limit=2)
    assert len(limited) == 2


async def test_round_trip_preserves_envelope(sp):
    store = sp.get_event_store()
    event_id = await store.append(
        event_type="session.steered", actor="user-7",
        payload={"instruction": "go"}, workspace_id="primer",
        session_id="sess-1", correlation_id="corr-9",
    )
    [event] = await store.read_after(event_id - 1)
    assert event.actor == "user-7"
    assert event.payload == {"instruction": "go"}
    assert event.session_id == "sess-1"
    assert event.correlation_id == "corr-9"
    assert event.occurred_at.tzinfo is not None


async def test_prune_honors_both_bounds(sp):
    """Retention deletes only rows old enough AND below the cursor floor."""
    store = sp.get_event_store()
    old = datetime.now(timezone.utc) - timedelta(days=60)
    stale_a = await store.append(event_type="a.created", occurred_at=old)
    stale_b = await store.append(event_type="b.created", occurred_at=old)
    fresh = await store.append(event_type="c.created")

    # keep_after_id below stale_b: only stale_a is eligible.
    removed = await store.prune(
        older_than=datetime.now(timezone.utc) - timedelta(days=30),
        keep_after_id=stale_a,
    )
    assert removed == 1
    remaining = [e.id for e in await store.read_after(0)]
    assert remaining == [stale_b, fresh]

    # Fresh rows survive any cursor bound: too new to prune.
    removed = await store.prune(
        older_than=datetime.now(timezone.utc) - timedelta(days=30),
        keep_after_id=fresh,
    )
    assert removed == 1
    assert [e.id for e in await store.read_after(0)] == [fresh]


async def test_cursors_default_missing_upsert_and_floor(sp):
    store = sp.get_event_store()
    assert await store.get_cursor("sub-1") is None
    assert await store.active_cursor_floor() is None
    await store.set_cursor("sub-1", 10)
    await store.set_cursor("sub-2", 4)
    await store.set_cursor("sub-1", 12)
    assert await store.get_cursor("sub-1") == 12
    assert await store.active_cursor_floor() == 4
    await store.delete_cursor("sub-2")
    assert await store.active_cursor_floor() == 12
    await store.delete_cursor("sub-2")  # idempotent
