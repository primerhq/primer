"""Strict occurrence-CAS tests for task-gate decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from primer.claim.tool_call_resume import (
    TaskGateResumeOutcome,
    durably_mark_tool_call_task_resumable,
)
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.yield_ import Yielded
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.task_gate_runtime import TaskGateState


TOKEN = "a" * 32
EVENT_KEY = "tool_approval:sess-1:call-1"


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _task(*, token=TOKEN, seq=1) -> ToolCallTask:
    now = datetime.now(UTC)
    gate = TaskGateState(
        occurrence_token=token,
        gate_seq=seq,
        event_key=EVENT_KEY,
        yielded=Yielded(
            tool_name="_approval",
            event_key=EVENT_KEY,
            resume_metadata={
                "original_call": {
                    "id": "call-1",
                    "name": "workspace__write",
                    "arguments": {},
                },
            },
        ),
        tool_call_id="call-1",
        parked_at=now,
        deadline=now + timedelta(minutes=5),
        binding_epoch=2,
    )
    return ToolCallTask(
        id="sess-1/x:tool:1:1",
        session_id="sess-1",
        turn_no=1,
        tool_name="workspace__write",
        state=ToolCallTaskState.GATED,
        record_seq=1,
        call_id="call-1",
        gate_event_key=EVENT_KEY,
        gate_until=gate.deadline,
        gate_state=gate.to_jsonable(),
        gate_occurrence_token=token,
        gate_seq=seq,
        created_at=now,
        started_at=now,
    )


class _Engine:
    def __init__(self, *, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first

    async def mark_resumable(self, kind, entity_id, **kwargs):
        assert (kind, entity_id) == (ClaimKind.TOOL_CALL, "sess-1/x:tool:1:1")
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("lease write failed")


async def _decide(storage, engine, *, token=TOKEN, seq=1, payload=None):
    return await durably_mark_tool_call_task_resumable(
        "sess-1/x:tool:1:1",
        occurrence_token=token,
        gate_seq=seq,
        event_key=EVENT_KEY,
        payload=payload or {"decision": "approved"},
        task_storage=storage,
        engine=engine,
    )


@pytest.mark.asyncio
async def test_decision_lands_receipt_and_rearms_exact_occurrence(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    await storage.create(_task())
    engine = _Engine()

    outcome = await _decide(storage, engine)

    assert outcome is TaskGateResumeOutcome.ADVANCED
    row = await storage.get("sess-1/x:tool:1:1")
    assert row.state is ToolCallTaskState.QUEUED
    assert row.claim_token is None
    assert row.gate_resolution == row.gate_state["resolution"]
    assert row.gate_resolution["payload"] == {"decision": "approved"}
    assert engine.calls == 1


@pytest.mark.asyncio
async def test_identical_retry_repairs_failed_lease_rearm(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    await storage.create(_task())
    engine = _Engine(fail_first=True)

    with pytest.raises(RuntimeError, match="lease write failed"):
        await _decide(storage, engine)
    row = await storage.get("sess-1/x:tool:1:1")
    assert row.state is ToolCallTaskState.QUEUED, "the receipt commits before lease re-arm"

    outcome = await _decide(storage, engine)
    assert outcome is TaskGateResumeOutcome.IDEMPOTENT
    assert engine.calls == 2


@pytest.mark.asyncio
async def test_conflicting_duplicate_is_refused_without_rearm(sqlite_provider):
    storage = sqlite_provider.get_storage(ToolCallTask)
    await storage.create(_task())
    engine = _Engine()
    assert await _decide(storage, engine) is TaskGateResumeOutcome.ADVANCED

    outcome = await _decide(storage, engine, payload={"decision": "rejected"})

    assert outcome is TaskGateResumeOutcome.REFUSED_CONFLICT
    assert engine.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement,expected",
    [
        (
            lambda row: row.model_copy(update={
                "state": ToolCallTaskState.RUNNING,
                "claim_token": "live-token",
            }),
            TaskGateResumeOutcome.REFUSED_RUNNING,
        ),
        (
            lambda row: row.model_copy(update={"state": ToolCallTaskState.FAILED}),
            TaskGateResumeOutcome.REFUSED_TERMINAL,
        ),
        (
            lambda row: _task(token="b" * 32, seq=2),
            TaskGateResumeOutcome.REFUSED_STALE,
        ),
    ],
)
async def test_live_terminal_and_later_occurrences_are_never_mutated(
    sqlite_provider, replacement, expected,
):
    storage = sqlite_provider.get_storage(ToolCallTask)
    initial = _task()
    await storage.create(initial)
    moved = replacement(initial)
    await storage.update(moved)
    engine = _Engine()

    outcome = await _decide(storage, engine)

    assert outcome is expected
    assert await storage.get(initial.id) == moved
    assert engine.calls == 0


@pytest.mark.asyncio
async def test_live_claim_winning_between_read_and_cas_is_not_cleared(
    sqlite_provider, monkeypatch,
):
    storage = sqlite_provider.get_storage(ToolCallTask)
    initial = _task()
    await storage.create(initial)
    original_patch = storage.patch_if_or_current

    async def _claim_wins(id, patch=None, *, where, set_paths=None, conn=None):
        current = await storage.get(id)
        await storage.update(current.model_copy(update={
            "state": ToolCallTaskState.RUNNING,
            "claim_token": "new-live-token",
        }))
        return await original_patch(
            id, patch, where=where, set_paths=set_paths, conn=conn,
        )

    monkeypatch.setattr(storage, "patch_if_or_current", _claim_wins)
    engine = _Engine()

    outcome = await _decide(storage, engine)

    assert outcome is TaskGateResumeOutcome.REFUSED_RUNNING
    row = await storage.get(initial.id)
    assert (row.state, row.claim_token) == (
        ToolCallTaskState.RUNNING, "new-live-token",
    )
    assert row.gate_resolution is None
    assert engine.calls == 0
