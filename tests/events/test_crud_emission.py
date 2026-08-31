"""Storage-layer CRUD event emission (SQLite backend).

Registered kinds append ``<kind>.created/updated/deleted`` in the same
transaction as the row write; unregistered kinds stay silent; a failed
or rolled-back write leaves no event behind.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import pytest
import pytest_asyncio

from primer.events.registry import kind_for_model, register_event_kind
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


class EvWidget(Identifiable):
    """Registered kind: writes must emit."""

    _id_prefix: ClassVar[str | None] = "evwidget"
    name: str = ""


class EvGadget(Identifiable):
    """Deliberately unregistered: writes must stay silent."""

    _id_prefix: ClassVar[str | None] = "evgadget"
    name: str = ""


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    # Registered per-test (idempotent) rather than at module import so
    # another suite's registry _reset_for_test() between import and
    # execution cannot unregister the kind.
    register_event_kind("evwidget", EvWidget)
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_registered_kind_emits_created_updated_deleted(sp):
    storage = sp.get_storage(EvWidget)
    created = await storage.create(EvWidget(id="w1", name="first"))
    created.name = "second"
    await storage.update(created)
    await storage.delete("w1")

    events = await sp.get_event_store().read_after(0)
    assert [e.event_type for e in events] == [
        "evwidget.created", "evwidget.updated", "evwidget.deleted",
    ]
    assert all(e.entity_kind == "evwidget" for e in events)
    assert all(e.entity_id == "w1" for e in events)
    assert events[0].payload["name"] == "first"
    assert events[1].payload["name"] == "second"
    assert events[2].payload == {"id": "w1"}


async def test_unregistered_kind_emits_nothing(sp):
    assert kind_for_model(EvGadget) is None
    storage = sp.get_storage(EvGadget)
    await storage.create(EvGadget(id="g1", name="quiet"))
    await storage.delete("g1")
    # The events table may not even exist yet; ensure then read.
    await sp.get_event_store().ensure_schema()
    assert await sp.get_event_store().read_after(0) == []


async def test_failed_create_emits_nothing(sp):
    storage = sp.get_storage(EvWidget)
    await storage.create(EvWidget(id="dup", name="one"))
    with pytest.raises(ConflictError):
        await storage.create(EvWidget(id="dup", name="two"))
    events = await sp.get_event_store().read_after(0)
    assert [e.event_type for e in events] == ["evwidget.created"]


async def test_rollback_discards_event_with_the_row(sp):
    """The event rides the caller's transaction, both ways."""
    storage = sp.get_storage(EvWidget)

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        async with sp.transaction() as conn:
            await storage.create(EvWidget(id="w2", name="doomed"), conn=conn)
            raise _Boom()

    assert await storage.get("w2") is None
    # The rollback also discarded the lazily-created events table;
    # re-ensure (as the lifespan does at boot) before reading.
    await sp.get_event_store().ensure_schema()
    events = await sp.get_event_store().read_after(0)
    assert [e for e in events if e.entity_id == "w2"] == []


async def test_payload_matches_stored_dump_exactly(sp):
    """The payload is the row's data column verbatim: the id lives in
    its own column (and the event envelope), not in the dump."""
    storage = sp.get_storage(EvWidget)
    await storage.create(EvWidget(id="w3", name="exact"))
    [event] = await sp.get_event_store().read_after(0)
    stored = await storage.get("w3")
    assert event.payload == {"name": "exact"}
    assert event.payload["name"] == stored.name
    assert event.entity_id == "w3"
