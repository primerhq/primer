"""Tests for entity-storage state-transition logic inside each ClaimAdapter.

Each adapter owns its on_release logic internally using Storage[T].get /
Storage[T].update — no per-entity storage subclass is needed.
"""

from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.harness import Harness, HarnessOperation, HarnessStatus
from primer.model.chats import Chat
from primer.model.workspace_session import WorkspaceSession, SessionStatus, AgentSessionBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.chats import ChatClaimAdapter
from primer.claim.adapters.harnesses import HarnessClaimAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path):
    cfg = SqliteConfig(path=tmp_path / "data.sqlite")
    provider = SqliteStorageProvider(cfg)
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


# ---------------------------------------------------------------------------
# SessionClaimAdapter.on_release
# ---------------------------------------------------------------------------

def _make_session(id: str = "sess-1") -> WorkspaceSession:
    return WorkspaceSession(
        id=id,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="agent-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC),
        turn_no=3,
        last_worker_id="old-worker",
        parked_status="resumable",
        parked_event_key="timer:abc",
        parked_until=datetime.now(UTC),
        parked_at=datetime.now(UTC),
        parked_state={"foo": "bar"},
    )


@pytest.mark.asyncio
async def test_session_on_release_success_bumps_turn_no_and_clears_park(sqlite_provider):
    storage = sqlite_provider.get_storage(WorkspaceSession)
    sess = _make_session()
    await storage.create(sess)

    adapter = SessionClaimAdapter(session_storage=storage)
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    await adapter.on_release(conn=None, entity_id="sess-1", outcome=outcome)

    updated = await storage.get("sess-1")
    assert updated is not None
    assert updated.turn_no == 4
    assert updated.parked_status is None
    assert updated.parked_event_key is None
    assert updated.parked_until is None
    assert updated.parked_state is None
    assert updated.last_worker_id is None  # outcome has no worker_id


@pytest.mark.asyncio
async def test_session_on_release_failure_still_clears_park(sqlite_provider):
    storage = sqlite_provider.get_storage(WorkspaceSession)
    sess = _make_session()
    await storage.create(sess)

    adapter = SessionClaimAdapter(session_storage=storage)
    outcome = ReleaseOutcome(success=False, last_error="something failed")
    await adapter.on_release(conn=None, entity_id="sess-1", outcome=outcome)

    updated = await storage.get("sess-1")
    assert updated is not None
    # Park / worker fields cleared (bookkeeping).
    assert updated.parked_status is None
    assert updated.parked_event_key is None
    assert updated.parked_state is None
    # turn_no MUST NOT bump on failure — only successful turns advance
    # the counter. Bug 5 of the diagnostic report fixed this.
    assert updated.turn_no == 3
    assert updated.last_turn_at is None


@pytest.mark.asyncio
async def test_session_on_release_missing_entity_returns_silently(sqlite_provider):
    storage = sqlite_provider.get_storage(WorkspaceSession)
    adapter = SessionClaimAdapter(session_storage=storage)
    # Should not raise — entity does not exist
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    await adapter.on_release(conn=None, entity_id="nonexistent", outcome=outcome)


@pytest.mark.asyncio
async def test_session_on_release_none_storage_raises(sqlite_provider):
    adapter = SessionClaimAdapter(session_storage=None)
    outcome = ReleaseOutcome(success=True)
    with pytest.raises(RuntimeError, match="session_storage"):
        await adapter.on_release(conn=None, entity_id="sess-1", outcome=outcome)


# ---------------------------------------------------------------------------
# ChatClaimAdapter.on_release
# ---------------------------------------------------------------------------

def _make_chat(id: str = "chat-1") -> Chat:
    return Chat(
        id=id,
        agent_id="agent-1",
        created_at=datetime.now(UTC),
        status="active",
        turn_status="running",
    )


@pytest.mark.asyncio
async def test_chat_on_release_success_drop_sets_idle(sqlite_provider):
    storage = sqlite_provider.get_storage(Chat)
    chat = _make_chat()
    await storage.create(chat)

    adapter = ChatClaimAdapter(chat_storage=storage)
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    await adapter.on_release(conn=None, entity_id="chat-1", outcome=outcome)

    updated = await storage.get("chat-1")
    assert updated is not None
    assert updated.turn_status == "idle"


@pytest.mark.asyncio
async def test_chat_on_release_success_no_drop_sets_claimable(sqlite_provider):
    storage = sqlite_provider.get_storage(Chat)
    chat = _make_chat()
    await storage.create(chat)

    adapter = ChatClaimAdapter(chat_storage=storage)
    outcome = ReleaseOutcome(success=True, drop_lease=False)
    await adapter.on_release(conn=None, entity_id="chat-1", outcome=outcome)

    updated = await storage.get("chat-1")
    assert updated is not None
    assert updated.turn_status == "claimable"


@pytest.mark.asyncio
async def test_chat_on_release_failure_sets_claimable(sqlite_provider):
    storage = sqlite_provider.get_storage(Chat)
    chat = _make_chat()
    await storage.create(chat)

    adapter = ChatClaimAdapter(chat_storage=storage)
    outcome = ReleaseOutcome(success=False, last_error="boom")
    await adapter.on_release(conn=None, entity_id="chat-1", outcome=outcome)

    updated = await storage.get("chat-1")
    assert updated is not None
    assert updated.turn_status == "claimable"


@pytest.mark.asyncio
async def test_chat_on_release_missing_entity_returns_silently(sqlite_provider):
    storage = sqlite_provider.get_storage(Chat)
    adapter = ChatClaimAdapter(chat_storage=storage)
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    await adapter.on_release(conn=None, entity_id="nonexistent", outcome=outcome)


@pytest.mark.asyncio
async def test_chat_on_release_none_storage_raises(sqlite_provider):
    adapter = ChatClaimAdapter(chat_storage=None)
    outcome = ReleaseOutcome(success=True)
    with pytest.raises(RuntimeError, match="chat_storage"):
        await adapter.on_release(conn=None, entity_id="chat-1", outcome=outcome)


# ---------------------------------------------------------------------------
# HarnessClaimAdapter.on_release
# ---------------------------------------------------------------------------

def _make_harness(id: str = "harness-1") -> Harness:
    return Harness(
        id=id,
        slug="my-harness",
        name="My Harness",
        git_url="https://github.com/example/repo",
        status=HarnessStatus.DRAFT,
        pending_operation=HarnessOperation.SYNC,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_harness_on_release_success_sets_ready_and_clears_operation(sqlite_provider):
    storage = sqlite_provider.get_storage(Harness)
    harness = _make_harness()
    await storage.create(harness)

    adapter = HarnessClaimAdapter(harness_storage=storage)
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    await adapter.on_release(conn=None, entity_id="harness-1", outcome=outcome)

    updated = await storage.get("harness-1")
    assert updated is not None
    assert updated.pending_operation is None
    assert updated.status == HarnessStatus.READY
    assert updated.last_operation_error is None
    assert updated.last_operation_at is not None


@pytest.mark.asyncio
async def test_harness_on_release_failure_sets_error_and_records_error_msg(sqlite_provider):
    storage = sqlite_provider.get_storage(Harness)
    harness = _make_harness()
    await storage.create(harness)

    adapter = HarnessClaimAdapter(harness_storage=storage)
    outcome = ReleaseOutcome(success=False, last_error="git clone failed", drop_lease=True)
    await adapter.on_release(conn=None, entity_id="harness-1", outcome=outcome)

    updated = await storage.get("harness-1")
    assert updated is not None
    assert updated.pending_operation is None
    assert updated.status == HarnessStatus.ERROR
    assert updated.last_operation_error == "git clone failed"
    assert updated.last_operation_at is not None


@pytest.mark.asyncio
async def test_harness_on_release_missing_entity_returns_silently(sqlite_provider):
    storage = sqlite_provider.get_storage(Harness)
    adapter = HarnessClaimAdapter(harness_storage=storage)
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    await adapter.on_release(conn=None, entity_id="nonexistent", outcome=outcome)


@pytest.mark.asyncio
async def test_harness_on_release_none_storage_raises(sqlite_provider):
    adapter = HarnessClaimAdapter(harness_storage=None)
    outcome = ReleaseOutcome(success=True)
    with pytest.raises(RuntimeError, match="harness_storage"):
        await adapter.on_release(conn=None, entity_id="harness-1", outcome=outcome)
