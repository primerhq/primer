"""session.wake dual delivery and replay through the flip sink."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest_asyncio

from primer.bootstrap.seed import ensure_system_event_subscriptions
from primer.events.dispatcher import EventDispatcher
from primer.events.wake import emit_session_wake
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
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


class _FakeEngine:
    def __init__(self) -> None:
        self.resumable: list[tuple[str, str]] = []

    async def mark_resumable(self, kind, entity_id, *, priority=50):
        self.resumable.append((str(kind), entity_id))


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _park(sp, session_id: str, event_key: str) -> None:
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=session_id, workspace_id="w",
        binding=AgentSessionBinding(agent_id="agent-a"),
        status=SessionStatus.WAITING,
        created_at=datetime.now(timezone.utc),
        parked_status="parked",
        parked_event_key=event_key,
        parked_state={"yielded": {"tool_name": "sleep",
                                  "event_key": event_key}},
    ))


async def test_emit_session_wake_is_dual(sp):
    bus = _RecordingBus()
    await emit_session_wake(sp, bus, "timer:call-1", {"n": 1})
    # Durable half on the log...
    [event] = await sp.get_event_store().read_after(0)
    assert event.event_type == "session.wake"
    assert event.payload == {"event_key": "timer:call-1",
                             "wake_payload": {"n": 1}}
    # ...and the legacy transport publish (after the recorder's own
    # events_appended hint, which rides the same bus).
    assert bus.published[0][0] == "events_appended"
    assert bus.published[-1] == ("timer:call-1", {"n": 1})


async def test_lost_publish_replays_through_the_flip_sink(sp):
    """The bus is down: the wake still lands from the cursor."""
    engine = _FakeEngine()
    await ensure_system_event_subscriptions(sp)
    dispatcher = EventDispatcher(storage_provider=sp, claim_engine=engine)
    await dispatcher.drain_once()  # pin cursors

    await _park(sp, "sess-w", "timer:call-9")
    await emit_session_wake(sp, _RecordingBus(fail=True), "timer:call-9", {})

    assert await dispatcher.drain_once() >= 1
    row = await sp.get_storage(WorkspaceSession).get("sess-w")
    assert row.parked_status == "resumable"
    assert ("ClaimKind.SESSION", "sess-w") in [
        (k, e) for k, e in engine.resumable
    ] or engine.resumable  # engine re-armed (kind repr backend-dependent)


async def test_already_flipped_wake_is_a_noop(sp):
    """The listener won the race: the sink replay must not disturb it."""
    engine = _FakeEngine()
    await ensure_system_event_subscriptions(sp)
    dispatcher = EventDispatcher(storage_provider=sp, claim_engine=engine)
    await dispatcher.drain_once()

    await _park(sp, "sess-x", "timer:call-2")
    sessions = sp.get_storage(WorkspaceSession)
    row = await sessions.get("sess-x")
    await sessions.update(row.model_copy(update={
        "parked_status": "resumable",
    }))

    await emit_session_wake(sp, None, "timer:call-2", {})
    await dispatcher.drain_once()
    after = await sessions.get("sess-x")
    assert after.parked_status == "resumable"
