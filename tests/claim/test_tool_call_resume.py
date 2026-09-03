"""Tests for durably_mark_tool_call_task_resumable (01a0518b, ruling 3).

Real SQLite storage (not a hand-rolled fake) because the guard's
correctness depends on Storage.update_unless's actual atomic-guard
semantics, not just a plausible-looking mock.
"""

from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path

import pytest
import pytest_asyncio

from primer.claim.in_memory import InMemoryClaimEngine
from primer.claim.tool_call_resume import durably_mark_tool_call_task_resumable
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path):
    cfg = SqliteConfig(path=tmp_path / "data.sqlite")
    provider = SqliteStorageProvider(cfg)
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _make_task(
    id: str = "worker:tool:1:1", *, state: ToolCallTaskState = ToolCallTaskState.GATED,
) -> ToolCallTask:
    return ToolCallTask(
        id=id,
        session_id="sess-1",
        turn_no=1,
        tool_name="workspace__write",
        state=state,
        record_seq=1,
        gate_event_key="tool_approval:sess-1:worker:tool:1:1",
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_flips_gated_to_queued_and_stashes_payload(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    task = _make_task()
    await storage.create(task)
    engine = InMemoryClaimEngine(adapters={})

    did = await durably_mark_tool_call_task_resumable(
        task,
        event_key=task.gate_event_key,
        payload={"decision": "approved", "reason": "looks fine"},
        task_storage=storage,
        engine=engine,
    )

    assert did is True
    updated = await storage.get(task.id)
    assert updated.state == ToolCallTaskState.QUEUED
    assert updated.gate_state["resume_event_payload"] == {
        "decision": "approved", "reason": "looks fine",
    }
    assert updated.gate_state["resume_event_key"] == task.gate_event_key


@pytest.mark.asyncio
async def test_rearms_the_claim_lease(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    task = _make_task()
    await storage.create(task)
    engine = InMemoryClaimEngine(adapters={})

    await durably_mark_tool_call_task_resumable(
        task, event_key="k", payload=None, task_storage=storage, engine=engine,
    )

    row = engine._leases[(ClaimKind.TOOL_CALL, task.id)]
    assert row.priority_score == 50  # ruling A: fixed default priority


@pytest.mark.asyncio
async def test_none_engine_still_flips_storage(sqlite_provider):
    """Mirrors durably_mark_session_resumable's own contract: no engine
    wired (e.g. the lightweight test app) still lands the durable flip,
    the lease re-arm is simply skipped."""
    storage = sqlite_provider.get_storage(ToolCallTask)
    task = _make_task()
    await storage.create(task)

    did = await durably_mark_tool_call_task_resumable(
        task, event_key="k", payload=None, task_storage=storage, engine=None,
    )

    assert did is True
    updated = await storage.get(task.id)
    assert updated.state == ToolCallTaskState.QUEUED


@pytest.mark.asyncio
async def test_rejects_when_not_gated(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    task = _make_task(state=ToolCallTaskState.QUEUED)
    await storage.create(task)
    engine = InMemoryClaimEngine(adapters={})

    did = await durably_mark_tool_call_task_resumable(
        task, event_key="k", payload=None, task_storage=storage, engine=engine,
    )

    assert did is False
    unchanged = await storage.get(task.id)
    assert unchanged.state == ToolCallTaskState.QUEUED


@pytest.mark.asyncio
async def test_double_fire_after_terminal_is_rejected_not_resurrected(sqlite_provider):
    """The update_unless guard's actual job: a second decision arriving
    after the row already reached DONE (e.g. a re-delivered bus
    notification, an operator double-click) must not resurrect it."""
    storage = sqlite_provider.get_storage(ToolCallTask)
    task = _make_task()
    await storage.create(task)
    engine = InMemoryClaimEngine(adapters={})

    # First flip succeeds (still GATED in the DB).
    ok = await durably_mark_tool_call_task_resumable(
        task, event_key="k", payload={"decision": "approved"},
        task_storage=storage, engine=engine,
    )
    assert ok is True

    # Simulate the task racing to DONE before the caller's stale `task`
    # snapshot (still showing GATED) gets used for a second flip attempt.
    done = task.model_copy(update={
        "state": ToolCallTaskState.DONE, "gate_state": None,
    })
    await storage.update(done)

    did_again = await durably_mark_tool_call_task_resumable(
        task, event_key="k", payload={"decision": "approved"},
        task_storage=storage, engine=engine,
    )

    assert did_again is False
    still_done = await storage.get(task.id)
    assert still_done.state == ToolCallTaskState.DONE
