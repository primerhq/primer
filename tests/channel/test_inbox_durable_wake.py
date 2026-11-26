"""6.1c: a channel reply leaves a durable wake, not only a bus publish.

ChannelInbox.handle_response used to publish the reply onto the wake bus and
nothing else: if that delivery was lost (the listener's mailbox overflowed,
a NOTIFY never reached the listener), the parked session waited until its
timeout. The reply is now recorded as a durable ``session.wake`` event (the
dual delivery the mcp-task bridge and the timeout sweeper already use): the
dispatcher's flip sink replays it when the volatile path was lost.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.adapter import ResponseEnvelope
from primer.channel.inbox import ChannelInbox
from primer.events.dispatcher import EventDispatcher
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


class _BrokenBus:
    """A bus whose volatile delivery is LOST (every publish raises)."""

    async def publish(self, event_key: str, payload: dict | None = None) -> None:
        raise RuntimeError("bus delivery lost")


def _parked_row(session_id: str, event_key: str) -> WorkspaceSession:
    now = datetime.now(timezone.utc)
    return WorkspaceSession(
        id=session_id,
        workspace_id="ws-1",
        binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING,
        created_at=now,
        parked_status="parked",
        parked_at=now,
        parked_event_key=event_key,
        parked_state={
            "tool_call_id": "tc-1",
            "yielded": {"tool_name": "ask_user", "event_key": event_key},
        },
    )


def _ask_env(session_id: str) -> ResponseEnvelope:
    return ResponseEnvelope(
        kind="ask_user",
        workspace_id="ws-1",
        session_id=session_id,
        tool_call_id="tc-1",
        response="42",
        decision=None,
        reason=None,
    )


async def test_a_channel_reply_leaves_a_durable_wake_when_the_bus_is_lost(sp) -> None:
    """The volatile publish is lost; the durable copy in the event log
    still wakes the parked session (mailbox overflow, dead NOTIFY)."""
    key = "ask_user:s-1:tc-1"
    await sp.get_storage(WorkspaceSession).create(_parked_row("s-1", key))

    inbox = ChannelInbox(event_bus=_BrokenBus(), storage_provider=sp)
    await inbox.handle_response(_ask_env("s-1"))  # the volatile publish raised and was tolerated

    # The reply is recorded durably under the park's key.
    store = sp.get_event_store()
    events = await store.read_after(0, limit=50)
    wakes = [
        e for e in events
        if e.event_type == "session.wake" and (e.payload or {}).get("event_key") == key
    ]
    assert wakes, "the reply must be recorded in the event log"
    assert wakes[-1].payload["wake_payload"] == {"response": "42"}

    # Replaying the recorded event through the dispatcher's flip sink
    # (the seeded session.wake subscription's delivery path) flips the park.
    d = EventDispatcher(storage_provider=sp)
    assert await d._deliver_flip(wakes[-1]) is True
    row = await sp.get_storage(WorkspaceSession).get("s-1")
    assert row.parked_status == "resumable"


async def test_the_volatile_publish_still_happens(sp) -> None:
    """Dual, not either/or: the bus still gets the wake for the transport-fast path."""
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        key = "ask_user:s-1:tc-1"
        await sp.get_storage(WorkspaceSession).create(_parked_row("s-1", key))
        sub = bus.subscribe()
        try:
            inbox = ChannelInbox(event_bus=bus, storage_provider=sp)
            await inbox.handle_response(_ask_env("s-1"))
            # The durable emit also publishes the ``events_appended`` hint;
            # consume until the wake itself.
            event = None
            for _ in range(3):
                event = await asyncio.wait_for(anext(sub), timeout=1.0)
                if event.event_key == key:
                    break
            assert event.event_key == key
            assert event.payload["response"] == "42"
        finally:
            await sub.aclose()
    finally:
        await bus.aclose()


async def test_without_a_storage_provider_the_reply_is_volatile_only() -> None:
    """A lightweight app (no storage provider) keeps the legacy behaviour:
    the bus publish, no event log to record into."""
    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        sub = bus.subscribe()
        try:
            inbox = ChannelInbox(event_bus=bus)
            await inbox.handle_response(_ask_env("s-1"))
            event = await asyncio.wait_for(anext(sub), timeout=1.0)
            assert event.event_key == "ask_user:s-1:tc-1"
        finally:
            await sub.aclose()
    finally:
        await bus.aclose()
