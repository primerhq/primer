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
from primer.model.workspace_session import WorkspaceSession, SessionStatus, AgentSessionBinding
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.int.claim import ParkRequest
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState


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


# ---------------------------------------------------------------------------
# ToolCallClaimAdapter.on_release (Phase 3 stage 7a, 01a0518b) - real
# storage round-trip, complementing test_tool_call_adapter.py's FakeStorage
# unit tests with proof the model actually persists through the real
# Storage layer (JSONB serialization, table auto-creation on first write).
# ---------------------------------------------------------------------------

def _make_tool_call_task(id: str = "worker:tool:1:1") -> ToolCallTask:
    return ToolCallTask(
        id=id,
        session_id="sess-1",
        turn_no=1,
        tool_name="workspace__write",
        state=ToolCallTaskState.RUNNING,
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_tool_call_on_release_success_sets_done(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    task = _make_tool_call_task()
    await storage.create(task)

    adapter = ToolCallClaimAdapter(task_storage=storage)
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    await adapter.on_release(conn=None, entity_id=task.id, outcome=outcome)

    updated = await storage.get(task.id)
    assert updated is not None
    assert updated.state == ToolCallTaskState.DONE
    assert updated.finished_at is not None
    assert updated.last_error is None


@pytest.mark.asyncio
async def test_tool_call_on_release_gate_sets_gated(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    task = _make_tool_call_task()
    await storage.create(task)

    adapter = ToolCallClaimAdapter(task_storage=storage)
    outcome = ReleaseOutcome(
        success=False, drop_lease=True,
        park=ParkRequest(
            parked_state={"kind": "approval"},
            parked_event_key="tool_approval:sess-1:worker:tool:1:1",
            parked_until=None,
            parked_at=datetime.now(UTC),
        ),
    )
    await adapter.on_release(conn=None, entity_id=task.id, outcome=outcome)

    updated = await storage.get(task.id)
    assert updated is not None
    assert updated.state == ToolCallTaskState.GATED
    assert updated.gate_event_key == "tool_approval:sess-1:worker:tool:1:1"
    assert updated.gate_state == {"kind": "approval"}


@pytest.mark.asyncio
async def test_tool_call_on_release_missing_entity_returns_silently(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    adapter = ToolCallClaimAdapter(task_storage=storage)
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    await adapter.on_release(conn=None, entity_id="nonexistent", outcome=outcome)


@pytest.mark.asyncio
async def test_tool_call_on_release_none_storage_raises(sqlite_provider):
    adapter = ToolCallClaimAdapter(task_storage=None)
    outcome = ReleaseOutcome(success=True)
    with pytest.raises(RuntimeError, match="task_storage"):
        await adapter.on_release(conn=None, entity_id="worker:tool:1:1", outcome=outcome)
