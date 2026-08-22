"""Dispatcher semantics: cursors, retries, skip-after-N, one-shot wakes.

Runs on the real SQLite provider so the cursor arithmetic is the
production arithmetic; sinks are exercised through fakes.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.events.dispatcher import EventDispatcher
from primer.model.event import (
    EventFilter,
    EventSubscription,
    LogSink,
    SessionWakeSink,
)
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage.sqlite import SqliteStorageProvider


async def _park_session(sp, session_id: str, event_key: str) -> None:
    """Persist a session row parked on ``event_key`` so the wake sink
    sees a deliverable park (the dispatcher refuses to wake sessions
    that are not visibly parked on the key)."""
    from datetime import datetime, timezone

    row = WorkspaceSession(
        id=session_id, workspace_id="w",
        binding=AgentSessionBinding(agent_id="agent-a"),
        status=SessionStatus.WAITING,
        created_at=datetime.now(timezone.utc),
        parked_status="parked",
        parked_event_key=event_key,
    )
    await sp.get_storage(WorkspaceSession).create(row)

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


class _RecordingBus:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.published: list[tuple[str, dict]] = []
        self._fail_times = fail_times

    async def publish(self, event_key, payload=None):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("bus down")
        self.published.append((event_key, payload or {}))


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _dispatcher(sp, bus=None, **kwargs) -> EventDispatcher:
    return EventDispatcher(storage_provider=sp, event_bus=bus, **kwargs)


async def _sub(sp, *, sink, filter_=None, sub_id="sub-1") -> EventSubscription:
    row = EventSubscription(
        id=sub_id, description="t",
        filter=filter_ or EventFilter(), sink=sink,
    )
    return await sp.get_storage(EventSubscription).create(row)


async def test_new_subscription_starts_at_head_not_zero(sp):
    store = sp.get_event_store()
    await store.append(event_type="session.steered")
    await store.append(event_type="session.steered")
    await _sub(sp, sink=LogSink())

    d = _dispatcher(sp)
    assert await d.drain_once() == 0  # history not replayed

    await store.append(event_type="session.steered")
    assert await d.drain_once() == 1
    assert await store.get_cursor("sub-1") == await store.max_id()


async def test_filter_gates_delivery_but_cursor_advances(sp):
    store = sp.get_event_store()
    await _sub(
        sp, sink=LogSink(),
        filter_=EventFilter(event_types=["agent.*"]),
    )
    d = _dispatcher(sp)
    await d.drain_once()  # pin the cursor at head (0 -> 0 here)

    await store.append(event_type="session.steered")
    await store.append(event_type="agent.created", entity_kind="agent",
                       entity_id="a1")
    assert await d.drain_once() == 1
    assert await store.get_cursor("sub-1") == await store.max_id()


async def test_failed_sink_holds_cursor_then_skips_after_n(sp):
    store = sp.get_event_store()
    bus = _RecordingBus(fail_times=99)
    await _sub(
        sp,
        sink=SessionWakeSink(event_key="evwait:s1:c1", session_id="s1",
                             one_shot=False),
    )
    await _park_session(sp, "s1", "evwait:s1:c1")
    d = _dispatcher(sp, bus=bus, max_failures=3)
    await d.drain_once()
    base_cursor = await store.get_cursor("sub-1")

    event_id = await store.append(event_type="session.steered")
    # Two failing passes: the cursor must not advance past the event.
    assert await d.drain_once() == 0
    assert await d.drain_once() == 0
    assert await store.get_cursor("sub-1") == base_cursor
    # Third pass hits max_failures: skipped, cursor moves on.
    assert await d.drain_once() == 0
    assert await store.get_cursor("sub-1") == event_id


async def test_one_shot_wake_publishes_and_completes(sp):
    store = sp.get_event_store()
    bus = _RecordingBus()
    await _sub(
        sp,
        sink=SessionWakeSink(event_key="evwait:s1:c1", session_id="s1"),
        filter_=EventFilter(event_types=["collection.document_pushed"]),
    )
    await _park_session(sp, "s1", "evwait:s1:c1")
    d = _dispatcher(sp, bus=bus)
    await d.drain_once()

    await store.append(
        event_type="collection.document_pushed",
        entity_kind="document", entity_id="d1",
        payload={"collection_id": "kb", "path": "guides/x"},
    )
    assert await d.drain_once() == 1

    [(key, payload)] = bus.published
    assert key == "evwait:s1:c1"
    assert payload["event_type"] == "collection.document_pushed"
    assert payload["payload"]["collection_id"] == "kb"
    # The one-shot subscription and its cursor are gone.
    subs = sp.get_storage(EventSubscription)
    assert await subs.get("sub-1") is None
    assert await store.get_cursor("sub-1") is None

    # A later event no longer delivers anywhere.
    await store.append(event_type="collection.document_pushed")
    assert await d.drain_once() == 0


async def test_paused_subscription_is_untouched(sp):
    store = sp.get_event_store()
    row = EventSubscription(
        id="sub-p", description="t", filter=EventFilter(),
        sink=LogSink(), paused=True,
    )
    await sp.get_storage(EventSubscription).create(row)
    d = _dispatcher(sp)
    await store.append(event_type="session.steered")
    assert await d.drain_once() == 0
    assert await store.get_cursor("sub-p") is None


async def test_converge_sink_calls_converge_entity(sp, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fake_converge(sp_, *, entity_type, entity_id, **kwargs):
        calls.append((entity_type, entity_id))
        return True

    import primer.knowledge.system_collection as sc
    monkeypatch.setattr(sc, "converge_entity", _fake_converge)

    from primer.model.event import ConvergeSink
    store = sp.get_event_store()
    await _sub(
        sp, sink=ConvergeSink(),
        filter_=EventFilter(event_types=["agent.*"]),
    )
    d = _dispatcher(sp)
    await d.drain_once()

    await store.append(event_type="agent.created", entity_kind="agent",
                       entity_id="a1")
    assert await d.drain_once() == 1
    assert calls == [("agent", "a1")]
