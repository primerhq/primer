"""M10: retention deletes in batches, and a cursor untouched for a day no longer holds it back."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest_asyncio

from primer.events.dispatcher import EventRetentionPruner
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "r.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_prune_deletes_in_batches_and_removes_the_same_rows(sp):
    store = sp.get_event_store()
    old = datetime.now(timezone.utc) - timedelta(days=60)
    ids = [await store.append(event_type="a.created", occurred_at=old) for _ in range(25)]
    fresh = await store.append(event_type="b.created")
    statements: list[str] = []
    await sp.connection.set_trace_callback(statements.append)

    removed = await store.prune(older_than=datetime.now(timezone.utc) - timedelta(days=30), keep_after_id=ids[-1], batch_size=10)

    assert removed == 25
    assert [e.id for e in await store.read_after(0)] == [fresh]
    assert len([s for s in statements if s.lstrip().upper().startswith("DELETE")]) == 3  # 10 + 10 + 5


async def test_a_stale_cursor_is_ignored_by_the_floor_only_when_asked(sp):
    store = sp.get_event_store()
    await store.set_cursor("live", 50)
    await store.set_cursor("orphan", 3)
    await sp.connection.execute(
        "UPDATE event_cursors SET updated_at = ? WHERE subscriber_id = 'orphan'",
        ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),),
    )
    await sp.connection.commit()
    assert await store.active_cursor_floor() == 3  # unchanged default
    assert await store.active_cursor_floor(stale_after=timedelta(days=1)) == 50


async def test_the_pruner_is_not_held_back_by_an_orphan_cursor(sp):
    store = sp.get_event_store()
    old = datetime.now(timezone.utc) - timedelta(days=60)
    ids = [await store.append(event_type="a.created", occurred_at=old) for _ in range(5)]
    await store.set_cursor("live", ids[-1])
    await store.set_cursor("orphan", ids[0])
    await sp.connection.execute(
        "UPDATE event_cursors SET updated_at = ? WHERE subscriber_id = 'orphan'",
        ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),),
    )
    await sp.connection.commit()

    removed = await EventRetentionPruner(storage_provider=sp, retention_days=30).prune_once()

    assert removed == 5
