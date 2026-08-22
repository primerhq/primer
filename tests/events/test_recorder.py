"""EventRecorder contract: catalog validation, swallow-vs-propagate,
and the best-effort hint."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.events.recorder import EVENTS_APPENDED_KEY, EventRecorder
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


class _RecordingBus:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, dict]] = []
        self._fail = fail

    async def publish(self, event_key, payload=None):
        if self._fail:
            raise RuntimeError("bus down")
        self.published.append((event_key, payload or {}))


class _BrokenStore:
    async def append(self, **kwargs):
        raise RuntimeError("db down")


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_emit_appends_and_hints(sp):
    bus = _RecordingBus()
    recorder = EventRecorder(sp.get_event_store(), bus)
    event_id = await recorder.emit(
        "session.steered", actor="user-1",
        payload={"instruction": "go"}, session_id="s1",
    )
    assert event_id is not None
    [event] = await sp.get_event_store().read_after(0)
    assert event.event_type == "session.steered"
    assert event.actor == "user-1"
    assert bus.published == [(EVENTS_APPENDED_KEY, {"max_id": event_id})]


async def test_unknown_type_raises_always(sp):
    recorder = EventRecorder(sp.get_event_store())
    with pytest.raises(ValueError, match="unknown event type"):
        await recorder.emit("session.typoed")


async def test_registered_crud_type_is_accepted(sp):
    # evwidget registered by the sibling module's import in-process;
    # register here independently so this file runs standalone too.
    from primer.events.registry import register_event_kind

    class _EvRecWidget:  # noqa: B903 - marker class, registry key only
        pass

    register_event_kind("evrecwidget", _EvRecWidget)
    recorder = EventRecorder(sp.get_event_store())
    assert await recorder.emit("evrecwidget.created") is not None


async def test_store_failure_swallowed_without_conn():
    recorder = EventRecorder(_BrokenStore())
    assert await recorder.emit("session.steered") is None


async def test_store_failure_propagates_with_conn():
    recorder = EventRecorder(_BrokenStore())
    with pytest.raises(RuntimeError, match="db down"):
        await recorder.emit("session.steered", conn=object())


async def test_bus_failure_never_reaches_the_caller(sp):
    recorder = EventRecorder(sp.get_event_store(), _RecordingBus(fail=True))
    assert await recorder.emit("session.steered") is not None
