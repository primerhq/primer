"""Which id the model is shown a tool result under, and which id the transcript records (S1b).

The assistant message in a parked history carries only the PROVIDER's raw id, so every ToolResultPart handed back
must carry it (``ToolCallTask.call_id``); the durable TOOL_RESULT record keeps the SCOPED id (the transcript's
convention); the row id is the session-qualified form and never leaves the system.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import primer.worker.tool_wait_resume_coordinator as coordinator
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.scheduler import WorkerConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState, tool_call_task_id
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ToolWaitParkedState
from tests.conftest import _FakeStorageProvider

T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
SID = "s1"


def _q(n: int) -> str:
    return tool_call_task_id(SID, f"x:tool:0:{n}")


def _task(n: int, state=ToolCallTaskState.DONE, *, call_id: str | None, result: dict | None,
          last_error: str | None = None) -> ToolCallTask:
    return ToolCallTask(
        id=_q(n), session_id=SID, turn_no=0, tool_name="t", state=state, record_seq=n, call_id=call_id,
        result_state=result, last_error=last_error, created_at=T0, finished_at=T0 + timedelta(seconds=n),
    )


class _Executor:
    def __init__(self) -> None:
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages: list[Message]) -> None:
        self.injected.append(list(messages))


async def _resume(monkeypatch, tasks: list[ToolCallTask], raw_ids: list[str]):
    provider = _FakeStorageProvider()
    for task in tasks:
        await provider.get_storage(ToolCallTask).create(task)
    parked = ToolWaitParkedState(
        outstanding_task_ids=[t.id for t in tasks], notifying_task_ids=[], event_key="tool_wait:s1:0:x",
        llm_messages=[Message(role="assistant", parts=[
            ToolCallPart(id=raw, name="t", arguments={}) for raw in raw_ids
        ]).model_dump(mode="json")],
        turn_no=0, started_at=T0,
    )
    session = WorkspaceSession(
        id=SID, workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.WAITING,
        turn_status="idle", parked_status="resumable", parked_state=parked.to_jsonable(), created_at=T0,
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=provider,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=None, event_bus=None,  # type: ignore[arg-type]
    )
    executor = _Executor()
    persisted: list[list[ToolCallTask]] = []

    async def _load(_ws):
        return None

    async def _build(_s, _w):
        return executor

    async def _persist(_pool, _session, tasks_, **_kw):
        persisted.append(list(tasks_))

    monkeypatch.setattr(pool, "_load_workspace_for_persist", _load)
    monkeypatch.setattr(pool, "_build_agent_executor", _build)
    monkeypatch.setattr(coordinator, "persist_resume_tool_result_records", _persist)
    lease = Lease(kind=ClaimKind.SESSION, entity_id=SID, claimed_by="w", claimed_at=T0, expires_at=T0,
                  attempt_count=1, last_error=None)
    await coordinator.resume_engine_tool_wait(pool, lease, session)
    (messages,) = executor.injected
    tool_use_ids = {p.id for m in messages if m.role == "assistant" for p in m.parts if isinstance(p, ToolCallPart)}
    return messages[-1].parts, tool_use_ids, persisted


@pytest.mark.asyncio
async def test_every_result_pairs_with_a_tool_use_by_the_providers_raw_id(monkeypatch):
    """Whatever id a result was written under (the raw id, the scoped id, even the qualified row id), the model is
    shown it under ``call_id``; mutation: hand the stored result back untouched."""
    tasks = [
        _task(1, call_id="call_a", result={"id": "call_a", "output": "A", "error": False}),
        _task(2, call_id="call_b", result={"id": "x:tool:0:2", "output": "B", "error": False}),
        _task(3, call_id="call_c", result={"id": _q(3), "output": "C", "error": False}),
    ]
    parts, tool_use_ids, _ = await _resume(monkeypatch, tasks, ["call_a", "call_b", "call_c"])

    assert [p.id for p in parts] == ["call_a", "call_b", "call_c"]
    assert {p.id for p in parts} == tool_use_ids, "a result with no tool_use (or the reverse) is rejected by the provider"
    assert [p.output for p in parts] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_a_synthesised_error_part_carries_the_raw_id_too(monkeypatch):
    tasks = [_task(1, ToolCallTaskState.FAILED, call_id="call_a", result=None, last_error="boom")]
    parts, tool_use_ids, _ = await _resume(monkeypatch, tasks, ["call_a"])

    (part,) = parts
    assert (part.id, part.output, part.error) == ("call_a", "boom", True)
    assert {part.id} == tool_use_ids


@pytest.mark.asyncio
async def test_a_row_without_call_id_keeps_the_previous_behaviour(monkeypatch):
    """A row written before the field existed: the stored result keeps the id it has, and a synthesised part uses the
    scoped id (never the session-qualified row id, which is internal)."""
    tasks = [
        _task(1, call_id=None, result={"id": "call_a", "output": "A", "error": False}),
        _task(2, ToolCallTaskState.FAILED, call_id=None, result=None, last_error="boom"),
    ]
    parts, _, _ = await _resume(monkeypatch, tasks, ["call_a", "call_b"])

    assert [p.id for p in parts] == ["call_a", "x:tool:0:2"]


@pytest.mark.asyncio
async def test_the_tasks_handed_to_the_record_writer_are_the_rows_the_blob_names(monkeypatch):
    tasks = [_task(1, call_id="call_a", result={"id": "call_a", "output": "A", "error": False})]
    _, _, persisted = await _resume(monkeypatch, tasks, ["call_a"])

    assert [[t.id for t in batch] for batch in persisted] == [[_q(1)]]
    assert persisted[0][0].scoped_call_id == "x:tool:0:1", "the writer records the scoped id (see the e2e)"


@pytest.mark.asyncio
async def test_the_resumed_history_serialises_for_anthropic_with_every_tool_use_paired(monkeypatch):
    """Anthropic rejects a request whose tool_use has no matching tool_result (or the reverse). Push the assembled
    resume through the real serialiser: a done task, a result written under the scoped id, and a poisoned task."""
    from primer.llm.anthropic import _messages_to_anthropic

    tasks = [
        _task(1, call_id="toolu_01", result={"id": "toolu_01", "output": "A", "error": False}),
        _task(2, call_id="toolu_02", result={"id": "x:tool:0:2", "output": "B", "error": False}),
        _task(3, ToolCallTaskState.FAILED, call_id="toolu_03", result=None, last_error="poisoned"),
    ]
    raw = ["toolu_01", "toolu_02", "toolu_03"]
    parts, _, _ = await _resume(monkeypatch, tasks, raw)

    assistant = Message(role="assistant", parts=[ToolCallPart(id=r, name="t", arguments={}) for r in raw])
    _, wire = _messages_to_anthropic([assistant, Message(role="tool", parts=list(parts))])

    used = [b["id"] for b in wire[0]["content"] if b["type"] == "tool_use"]
    answered = [b["tool_use_id"] for b in wire[1]["content"] if b["type"] == "tool_result"]
    assert used == raw
    assert sorted(answered) == sorted(used), f"unpaired: tool_use {used} vs tool_result {answered}"
