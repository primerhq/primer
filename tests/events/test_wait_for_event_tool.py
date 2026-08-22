"""wait_for_event: park bookkeeping, resume formatting, full wake loop."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest_asyncio

from primer.events.dispatcher import EventDispatcher
from primer.model.event import EventSubscription
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolContext, Yielded, YieldTimeout
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.events_wait import (
    make_wait_for_event_handler,
    wait_for_event_resume,
)

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
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


def _ctx(session_id="sess-1", tool_call_id="call-1") -> ToolContext:
    return ToolContext(
        tool_call_id=tool_call_id,
        session_id=session_id,
        workspace_id="w",
    )


async def test_handler_creates_pinned_one_shot_and_yields(sp):
    store = sp.get_event_store()
    await store.append(event_type="session.steered")  # pre-existing history
    handler = make_wait_for_event_handler(sp)

    result = await handler(
        {"event_types": ["collection.document_pushed"],
         "timeout_seconds": 60.0},
        ctx=_ctx(),
    )
    assert isinstance(result, Yielded)
    assert result.event_key == "evwait:sess-1:call-1"
    assert result.timeout == 60.0

    sub_id = result.resume_metadata["subscription_id"]
    sub = await sp.get_storage(EventSubscription).get(sub_id)
    assert sub is not None
    assert sub.sink.kind == "session_wake"
    assert sub.sink.one_shot is True
    assert sub.filter.event_types == ["collection.document_pushed"]
    # Cursor pinned at head: pre-existing history is out of scope.
    assert await store.get_cursor(sub_id) == await store.max_id()


async def test_handler_rejects_chat_surface_and_bad_regex(sp):
    handler = make_wait_for_event_handler(sp)
    no_session = await handler(
        {"event_types": ["*"]},
        ctx=ToolContext(tool_call_id="c", session_id=None,
                        workspace_id=None, chat_id="chat-1"),
    )
    assert no_session.is_error

    bad_regex = await handler(
        {"event_types": ["*"],
         "fields": [{"path": "payload.x", "op": "regex",
                     "value": "([unclosed"}]},
        ctx=_ctx(),
    )
    assert bad_regex.is_error
    # Nothing half-created.
    from primer.model.storage import OffsetPage

    page = await sp.get_storage(EventSubscription).list(OffsetPage(length=10))
    assert page.items == []


async def test_resume_formats_event_timeout(sp):
    envelope = {"event_type": "collection.document_pushed", "id": 7}
    out = wait_for_event_resume({}, envelope, None)
    assert not out.is_error
    assert '"event"' in out.output

    timed = wait_for_event_resume(
        {"event_types": ["*"]}, YieldTimeout(elapsed_seconds=5.0), None,
    )
    assert not timed.is_error
    assert "timed_out" in timed.output


async def test_full_wake_loop_through_the_dispatcher(sp):
    """Handler-created subscription + parked row + dispatcher = wake."""
    handler = make_wait_for_event_handler(sp)
    yielded = await handler(
        {"event_types": ["collection.document_pushed"],
         "fields": [{"path": "payload.collection_id", "op": "eq",
                     "value": "kb"}]},
        ctx=_ctx(),
    )
    assert isinstance(yielded, Yielded)

    # The worker would write this park after the tool returns.
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="sess-1", workspace_id="w",
        binding=AgentSessionBinding(agent_id="agent-a"),
        status=SessionStatus.WAITING,
        created_at=datetime.now(timezone.utc),
        parked_status="parked",
        parked_event_key=yielded.event_key,
    ))

    bus = _Bus()
    dispatcher = EventDispatcher(storage_provider=sp, event_bus=bus)
    store = sp.get_event_store()

    # A non-matching collection does not wake it.
    await store.append(
        event_type="collection.document_pushed",
        payload={"collection_id": "other", "path": "p"},
    )
    assert await dispatcher.drain_once() == 0
    assert bus.published == []

    # The matching one does, delivering the envelope on the park key.
    await store.append(
        event_type="collection.document_pushed",
        payload={"collection_id": "kb", "path": "guides/x"},
    )
    assert await dispatcher.drain_once() == 1
    [(key, payload)] = bus.published
    assert key == yielded.event_key
    assert payload["payload"]["collection_id"] == "kb"
    sub_id = yielded.resume_metadata["subscription_id"]
    assert await sp.get_storage(EventSubscription).get(sub_id) is None
