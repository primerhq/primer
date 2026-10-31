"""A wait_for_event wake does not depend on the event fitting a NOTIFY payload (core review finding 3).

The dispatcher published the whole redacted event on the bus; on Postgres the NOTIFY body is capped (about 8 KB), so a large event (an
``agent.updated`` carries the full row) failed five times and was skipped: the parked run never woke although its event happened. A big
envelope now wakes the park through storage, the way the durable flip path does; a small one still rides the bus.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio

from primer.bus.postgres import MAX_NOTIFY_PAYLOAD_BYTES
from primer.events.dispatcher import EventDispatcher
from primer.model.event import EventSubscription
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.events_wait import make_wait_for_event_handler
from tests._support.caller import ADMIN_CALLER
from tests.events._users import seed_account_of
from tests.events.test_wait_for_event_tool import _ctx


class _SizeLimitedBus:
    """A bus that refuses a body over the Postgres NOTIFY cap, as the real one does."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        import json

        if len(json.dumps({"event_key": event_key, "payload": payload or {}})) > MAX_NOTIFY_PAYLOAD_BYTES:
            raise RuntimeError("notify_payload_too_long")
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


async def _parked(sp) -> Yielded:
    yielded = await make_wait_for_event_handler(sp)({"event_types": ["agent.updated"]}, ctx=_ctx())
    assert isinstance(yielded, Yielded)
    await seed_account_of(sp, ADMIN_CALLER.initiated_by)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="sess-1", workspace_id="w", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=datetime.now(UTC), initiated_by=ADMIN_CALLER.initiated_by, parked_status="parked", parked_event_key=yielded.event_key,
        parked_state={"tool_call_id": "call-1", "yielded": {"tool_name": "wait_for_event", "event_key": yielded.event_key,
                                                           "resume_metadata": {"subscription_id": yielded.resume_metadata["subscription_id"]}}},
    ))
    return yielded


async def test_a_big_event_wakes_the_park_through_storage(sp):
    yielded = await _parked(sp)
    bus = _SizeLimitedBus()
    await sp.get_event_store().append(event_type="agent.updated", entity_kind="agent", entity_id="ag-1", payload={"instructions": "x" * 9000})
    assert await EventDispatcher(storage_provider=sp, event_bus=bus).drain_once() == 1
    row = await sp.get_storage(WorkspaceSession).get("sess-1")
    assert row.parked_status == "resumable"
    envelope = (row.parked_state or {}).get("resume_event_payload") or {}
    assert envelope.get("event_type") == "agent.updated" and len(envelope["payload"]["instructions"]) == 9000
    assert bus.published == []  # it never fit the bus
    assert await sp.get_storage(EventSubscription).get(yielded.resume_metadata["subscription_id"]) is None  # the one-shot completed


async def test_a_small_event_still_rides_the_bus(sp):
    yielded = await _parked(sp)
    bus = _SizeLimitedBus()
    await sp.get_event_store().append(event_type="agent.updated", entity_kind="agent", entity_id="ag-1", payload={"instructions": "short"})
    assert await EventDispatcher(storage_provider=sp, event_bus=bus).drain_once() == 1
    [(key, payload)] = bus.published
    assert key == yielded.event_key and payload["payload"]["instructions"] == "short"
