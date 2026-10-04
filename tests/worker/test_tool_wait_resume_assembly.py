"""resume_engine_tool_wait: how the tool-role message is assembled (Phase 3 stage 7a, slice S1-D).

The agent-only resume reads every sibling task by id and builds ONE tool-role message. Two things must not
depend on timing or on a missing result: the ORDER of the parts (the order the park recorded, not the order
the tasks happened to finish, which the 7a executor makes arbitrary), and what a task that owes the model a
result but has none looks like (a synthesised error part, never a dropped id, which would leave a dangling
tool_use). Both are pinned here before the executor can make them matter.

Two things the fixture glosses over, so a reader does not take more from these tests than they say:

* The ORDER is the park's: outstanding ids, then the inline-answered ones. The executor's park must therefore
  record the outstanding ids in the model's call order (Ollama pairs same-name calls by position; every other
  adapter pairs by id and does not care). Only the model's interleaving of an inline call with a claimable one is
  lost, which no adapter depends on.
* IDS. The fixture gives the park's tool_use ids and the task ids the same strings. In production the park's
  messages carry the provider's raw ids while a task id is the scoped one, so the synthesised error part (which
  uses ``task.id``) matches no tool_use there. That is the "Session-qualified ids" gap in claim-machine.md; the
  xfail below pins it so the fix flips it.
* The durable TOOL_RESULT records are stubbed here (only their order is pinned); for a task with no result the
  real writer records ``output=None`` and ``error`` from the task state, not the synthesised part's text.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import primer.worker.tool_wait_resume_coordinator as coordinator
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.scheduler import WorkerConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ToolWaitParkedState
from tests.conftest import _FakeStorageProvider

T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
A, B, C, N = "x:tool:0:1", "x:tool:0:2", "x:tool:0:3", "x:tool:0:4"


class _RecordingExecutor:
    def __init__(self) -> None:
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages: list[Message]) -> None:
        self.injected.append(list(messages))


def _task(task_id: str, state=ToolCallTaskState.DONE, *, result: ToolResultPart | None = "auto",
          finished: int = 0, last_error: str | None = None) -> ToolCallTask:
    if result == "auto":
        result = ToolResultPart(id=task_id, output=f"result of {task_id}", error=False)
    return ToolCallTask(
        id=task_id, session_id="s1", turn_no=0, tool_name="t", state=state, record_seq=1,
        result_state=result.model_dump(mode="json") if result is not None else None,
        last_error=last_error, created_at=T0, finished_at=T0 + timedelta(seconds=finished),
    )


async def _resume(monkeypatch, tasks: list[ToolCallTask], *, outstanding: list[str], notifying: list[str] = ()):
    """Park a session on ``outstanding`` + ``notifying`` over ``tasks`` and run the resume; returns what happened."""
    provider = _FakeStorageProvider()
    task_storage = provider.get_storage(ToolCallTask)
    for task in tasks:
        await task_storage.create(task)
    parked = ToolWaitParkedState(
        outstanding_task_ids=list(outstanding), notifying_task_ids=list(notifying),
        event_key="tool_wait:s1:0:x",
        llm_messages=[Message(role="assistant", parts=[
            ToolCallPart(id=tid, name="t", arguments={}) for tid in [*outstanding, *notifying]
        ]).model_dump(mode="json")],
        turn_no=0, started_at=T0,
    )
    session = WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING, turn_status="idle", parked_status="resumable",
        parked_state=parked.to_jsonable(), created_at=T0,
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=provider,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=None, event_bus=None,  # type: ignore[arg-type]
    )
    executor = _RecordingExecutor()
    ended: list[str] = []
    persisted: list[list[str]] = []

    async def _load_workspace(_ws_id):
        return None

    async def _build_executor(_s, _w):
        return executor

    async def _end_session(_session, *, reason):
        ended.append(reason)
        return "ended"

    async def _persist(_pool, _session, tasks_):
        persisted.append([t.id for t in tasks_])

    monkeypatch.setattr(pool, "_load_workspace_for_persist", _load_workspace)
    monkeypatch.setattr(pool, "_build_agent_executor", _build_executor)
    monkeypatch.setattr(pool, "_end_session", _end_session)
    monkeypatch.setattr(coordinator, "persist_resume_tool_result_records", _persist)
    lease = Lease(kind=ClaimKind.SESSION, entity_id="s1", claimed_by="w", claimed_at=T0, expires_at=T0,
                  attempt_count=1, last_error=None)
    outcome = await coordinator.resume_engine_tool_wait(pool, lease, session)
    return outcome, executor, ended, persisted


def _parts(executor: _RecordingExecutor) -> list[ToolResultPart]:
    (messages,) = executor.injected
    assert messages[-1].role == "tool"
    return list(messages[-1].parts)


# ---- order ------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assembly_order_is_batch_order_regardless_of_completion_order(monkeypatch):
    """C finished first, then A, then B; the model is shown A, B, C (the park's order), then the inline call.
    Mutation M8: iterate the tasks reversed."""
    tasks = [
        _task(A, finished=20), _task(B, finished=30), _task(C, finished=10), _task(N, finished=0),
    ]
    outcome, executor, ended, persisted = await _resume(monkeypatch, tasks, outstanding=[A, B, C], notifying=[N])

    assert outcome.success is True and outcome.drop_lease is False and ended == []
    assert [p.id for p in _parts(executor)] == [A, B, C, N]
    assert [p.output for p in _parts(executor)] == [f"result of {t}" for t in (A, B, C, N)]
    assert persisted == [[A, B, C, N]], "the durable TOOL_RESULT records are written in the same order"


@pytest.mark.asyncio
async def test_the_same_order_holds_when_the_park_lists_the_ids_the_other_way_round(monkeypatch):
    tasks = [_task(A, finished=1), _task(B, finished=2), _task(C, finished=3)]
    _, executor, _, persisted = await _resume(monkeypatch, tasks, outstanding=[C, B, A])

    assert [p.id for p in _parts(executor)] == [C, B, A], "the park decides the order, not the clock or the id"
    assert persisted == [[C, B, A]]


# ---- a task that owes the model a result and has none -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_task_with_no_result_gets_a_synthesised_error_part_not_a_dropped_id(monkeypatch):
    tasks = [
        _task(A),
        _task(B, ToolCallTaskState.FAILED, result=None, last_error="tool call failed after 5 attempts"),
        _task(C, ToolCallTaskState.FAILED, result=None, last_error=None),
    ]
    _, executor, ended, _ = await _resume(monkeypatch, tasks, outstanding=[A, B, C])

    assert ended == []
    parts = _parts(executor)
    assert [p.id for p in parts] == [A, B, C], "every tool_use gets its pair"
    assert (parts[0].output, parts[0].error) == (f"result of {A}", False)
    assert (parts[1].output, parts[1].error) == ("tool call failed after 5 attempts", True)
    assert (parts[2].output, parts[2].error) == ("tool call failed", True)


@pytest.mark.asyncio
async def test_a_failed_task_that_carries_a_result_uses_it_verbatim(monkeypatch):
    """The executor's poison path writes the synthesised error into result_state itself; the resume must
    show that, not replace it with its own fallback text."""
    poison = ToolResultPart(id=A, output='{"error": "tool call failed after 5 attempts"}', error=True)
    tasks = [_task(A, ToolCallTaskState.FAILED, result=poison, last_error="ignored")]
    _, executor, _, _ = await _resume(monkeypatch, tasks, outstanding=[A])

    (part,) = _parts(executor)
    assert (part.id, part.output, part.error) == (A, poison.output, True)


@pytest.mark.asyncio
async def test_a_done_task_with_no_result_is_also_answered_with_an_error_part(monkeypatch):
    """Not reachable through the adapter (a terminal release without a result still owes one); the guard is
    the same branch, so a DONE row that lost its result cannot leave a dangling tool_use either."""
    tasks = [_task(A, ToolCallTaskState.DONE, result=None)]
    _, executor, _, _ = await _resume(monkeypatch, tasks, outstanding=[A])

    (part,) = _parts(executor)
    assert (part.id, part.output, part.error) == (A, "tool call failed", True)


# ---- fail closed ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ToolCallTaskState.QUEUED, ToolCallTaskState.RUNNING, ToolCallTaskState.GATED])
async def test_a_non_terminal_sibling_ends_the_session_failed_and_injects_nothing(monkeypatch, state):
    tasks = [_task(A), _task(B, state, result=None)]
    outcome, executor, ended, persisted = await _resume(monkeypatch, tasks, outstanding=[A, B])

    assert outcome == "ended" and ended == ["failed"]
    assert executor.injected == [] and persisted == []


@pytest.mark.asyncio
async def test_a_missing_sibling_row_ends_the_session_failed_and_injects_nothing(monkeypatch):
    outcome, executor, ended, persisted = await _resume(monkeypatch, [_task(A)], outstanding=[A, B])

    assert outcome == "ended" and ended == ["failed"]
    assert executor.injected == [] and persisted == []


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="known gap (claim-machine.md, 'Session-qualified ids'): the synthesised part carries the scoped task id, "
    "the park's tool_use carries the provider's raw id, so the pair never matches in production. Flip this when "
    "the task row gains a call_id and the coordinator uses it.",
)
async def test_every_synthesised_part_pairs_with_a_tool_use_in_the_parks_own_messages(monkeypatch):
    """With the id conventions production really has (raw provider id in the park's history, scoped id on the task)."""
    scoped, raw = "x:tool:0:1", "call_9f2"
    provider = _FakeStorageProvider()
    await provider.get_storage(ToolCallTask).create(
        _task(scoped, ToolCallTaskState.FAILED, result=None, last_error="boom")
    )
    parked = ToolWaitParkedState(
        outstanding_task_ids=[scoped], notifying_task_ids=[], event_key="tool_wait:s1:0:x",
        llm_messages=[Message(role="assistant", parts=[ToolCallPart(id=raw, name="t", arguments={})]).model_dump(mode="json")],
        turn_no=0, started_at=T0,
    )
    session = WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.WAITING,
        turn_status="idle", parked_status="resumable", parked_state=parked.to_jsonable(), created_at=T0,
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=provider,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=None, event_bus=None,  # type: ignore[arg-type]
    )
    executor = _RecordingExecutor()

    async def _build_executor(_s, _w):
        return executor

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(pool, "_load_workspace_for_persist", _noop)
    monkeypatch.setattr(pool, "_build_agent_executor", _build_executor)
    monkeypatch.setattr(coordinator, "persist_resume_tool_result_records", _noop)
    lease = Lease(kind=ClaimKind.SESSION, entity_id="s1", claimed_by="w", claimed_at=T0, expires_at=T0,
                  attempt_count=1, last_error=None)
    await coordinator.resume_engine_tool_wait(pool, lease, session)

    (messages,) = executor.injected
    tool_use_ids = {p.id for m in messages if m.role == "assistant" for p in m.parts if isinstance(p, ToolCallPart)}
    result_ids = {p.id for p in messages[-1].parts}
    assert result_ids <= tool_use_ids, f"results {result_ids} do not pair with tool_use {tool_use_ids}"
