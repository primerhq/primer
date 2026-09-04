"""End-to-end test for the tool-dispatch seam split's tool_wait park
(Phase 3 stage 7a, 01a0518b) - the "arc's summit" the leader asked for a
dedicated required test on.

Covers, in one flow:
  1. WRITE side (primer.session.dispatch's ``except ToolWaitPark``
     branch): a mixed batch (two claimable calls + one notifying call)
     answers the notifying call inline AND creates a ``ToolCallTask``
     row for every call in the batch - QUEUED for the claimable pair,
     terminal DONE (with ``result_state`` populated) for the notifying
     one - plus a tool_wait-shaped ``parked_state``.
  2. READ side (primer.worker.tool_wait_resume_coordinator): once every
     sibling task is terminal (simulating the not-yet-built claim
     worker completing the two QUEUED tasks), the resume coordinator
     assembles BOTH the claimed results and the notifying result into a
     single tool-role message and hands it to the executor, then
     returns the "next claim runs an ordinary continuation turn"
     outcome.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease, ReleaseOutcome
from primer.model.chat import (
    Message,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.scheduler import WorkerConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolWaitPark
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.worker.pool import WorkerPool
from primer.worker.tool_wait_resume_coordinator import resume_engine_tool_wait

from tests.conftest import _FakeStorageProvider


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}
        self.append_calls = 0

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.append_calls += 1
        key = (session_id, "messages.jsonl")
        self._data[key] = self._data.get(key, b"") + line

    def read_lines(self, session_id: str) -> list[str]:
        raw = self._data.get((session_id, "messages.jsonl"), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]


class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


class _RecordingClaimEngine:
    """Records upsert calls for ClaimKind.TOOL_CALL - the write side must
    only register a lease for QUEUED (claimable) tasks, never for the
    already-terminal notifying one."""

    def __init__(self) -> None:
        self.upserted: list[tuple[ClaimKind, str]] = []

    async def upsert(self, kind: ClaimKind, entity_id: str, **kwargs) -> None:
        self.upserted.append((kind, entity_id))


class _ToolWaitExecutor:
    """Emits three tool calls (two claimable, one notifying - by
    convention of THIS test, not tool_manager.is_notifying, since
    run_one_session_turn never consults a tool_manager itself) then
    raises ToolWaitPark, mirroring exactly what primer.agent.loop's
    _dispatch_as_claims does at the real seam."""

    def __init__(self, park: ToolWaitPark) -> None:
        self._park = park

    async def invoke(self, messages, **kwargs):
        yield ToolCallStart(id="call_a", name="tool_a", index=0)
        yield ToolCallEnd(id="call_a", arguments={"x": 1}, index=0)
        yield ToolCallStart(id="call_b", name="tool_b", index=1)
        yield ToolCallEnd(id="call_b", arguments={"y": 2}, index=1)
        yield ToolCallStart(id="call_c", name="notify_tool", index=2)
        yield ToolCallEnd(id="call_c", arguments={}, index=2)
        raise self._park
        yield  # pragma: no cover - generator marker, unreachable


class _RecordingExecutor:
    """Stand-in for the resume-time agent executor - records the
    injected resume messages so the test can assert both results landed
    in the SAME tool-role message."""

    def __init__(self) -> None:
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages: list[Message]) -> None:
        self.injected.append(list(messages))


async def _async_return(value):
    return value


def _make_lease(session_id: str) -> Lease:
    now = _now()
    return Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=now, expires_at=now, attempt_count=1, last_error=None,
    )


def _build_pool(storage) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,  # type: ignore[arg-type]
        storage=storage,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=None,  # type: ignore[arg-type]
        event_bus=None,  # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-tool-wait-e2e"
    return pool


@pytest.mark.asyncio
async def test_mixed_batch_notifies_inline_and_resume_assembles_both_results(
    monkeypatch,
) -> None:
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)

    session = WorkspaceSession(
        id="s-tool-wait-1",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await session_storage.create(session)

    # ------------------------------------------------------------------
    # Part 1: WRITE side - dispatch a mixed batch through the tool_wait
    # park branch.
    # ------------------------------------------------------------------
    notify_result = ToolResultPart(id="x:tool:0:3", output="notified ok", error=False)
    park = ToolWaitPark(
        outstanding_task_ids=["x:tool:0:1", "x:tool:0:2"],
        event_key="tool_wait:x:tool:0:1",
        notifying_results=[("x:tool:0:3", notify_result)],
    )

    fake_io = _FakeWorkspaceIO()
    fake_bus = _FakeEventBus()
    claim_engine = _RecordingClaimEngine()

    async def _build_executor(_session: WorkspaceSession):
        return _ToolWaitExecutor(park)

    deps = SessionDispatchDeps(
        storage_provider=storage_provider,
        workspace_io=fake_io,
        event_bus=fake_bus,
        build_executor=_build_executor,
        claim_engine=claim_engine,
    )

    # Claim through a REAL SessionClaimAdapter-backed engine (not the bare
    # _make_lease helper) so releasing the outcome below actually writes
    # the park columns onto the row - on_release is what turns
    # ReleaseOutcome.park into session.parked_state/parked_status, not
    # run_one_session_turn itself.
    session_engine = InMemoryClaimEngine(adapters={
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
    })
    await session_engine.upsert(ClaimKind.SESSION, session.id)
    session_lease = next(
        l for l in await session_engine.claim_due("worker-1", max_count=10)
        if l.entity_id == session.id
    )

    outcome = await run_one_session_turn(session_lease, deps)
    await session_engine.release(session_lease, outcome=outcome)

    assert outcome.success is True
    assert outcome.drop_lease is True
    assert outcome.park is not None
    assert outcome.park.parked_event_key == "tool_wait:x:tool:0:1"

    # The notifying call's DONE row exists, result_state populated -
    # answered inline, durable home for the resume coordinator to read.
    notify_task = await task_storage.get("x:tool:0:3")
    assert notify_task is not None
    assert notify_task.state == ToolCallTaskState.DONE
    assert notify_task.tool_name == "notify_tool"
    assert notify_task.result_state == notify_result.model_dump(mode="json")

    # The two claimable calls are QUEUED, pointing at their own durable
    # TOOL_CALL record's seq, and registered with the claim engine - the
    # notifying (already-terminal) one is NOT.
    task_a = await task_storage.get("x:tool:0:1")
    task_b = await task_storage.get("x:tool:0:2")
    assert task_a.state == ToolCallTaskState.QUEUED
    assert task_a.tool_name == "tool_a"
    assert task_b.state == ToolCallTaskState.QUEUED
    assert task_b.tool_name == "tool_b"
    assert claim_engine.upserted == [
        (ClaimKind.TOOL_CALL, "x:tool:0:1"),
        (ClaimKind.TOOL_CALL, "x:tool:0:2"),
    ]

    lines = [json.loads(ln) for ln in fake_io.read_lines(session.id)]
    tool_call_lines = {
        ln["payload"]["id"]: ln for ln in lines
        if ln["kind"] == SessionMessageKind.TOOL_CALL
    }
    assert set(tool_call_lines) == {"x:tool:0:1", "x:tool:0:2", "x:tool:0:3"}
    # record_seq on each task row points at its OWN durable record's seq.
    assert task_a.record_seq == tool_call_lines["x:tool:0:1"]["seq"]
    assert task_b.record_seq == tool_call_lines["x:tool:0:2"]["seq"]
    assert notify_task.record_seq == tool_call_lines["x:tool:0:3"]["seq"]

    yielded_lines = [ln for ln in lines if ln["kind"] == SessionMessageKind.YIELDED]
    assert len(yielded_lines) == 1
    assert yielded_lines[0]["payload"]["kind"] == "tool_wait"

    row = await session_storage.get(session.id)
    assert row.parked_state["kind"] == "tool_wait"
    assert row.parked_state["outstanding_task_ids"] == ["x:tool:0:1", "x:tool:0:2"]
    assert row.parked_state["notifying_task_ids"] == ["x:tool:0:3"]

    # ------------------------------------------------------------------
    # Part 2: simulate the (not-yet-built) claim worker completing the
    # two QUEUED tasks, mirroring what ToolCallClaimAdapter.on_release's
    # terminal branch expects to find already written (result_state set
    # BEFORE release/on_release runs - see that adapter's own docstring).
    # ------------------------------------------------------------------
    result_a = ToolResultPart(id="x:tool:0:1", output="result A", error=False)
    result_b = ToolResultPart(id="x:tool:0:2", output="result B", error=False)
    await task_storage.update(task_a.model_copy(update={
        "state": ToolCallTaskState.DONE,
        "result_state": result_a.model_dump(mode="json"),
        "finished_at": _now(),
    }))
    await task_storage.update(task_b.model_copy(update={
        "state": ToolCallTaskState.DONE,
        "result_state": result_b.model_dump(mode="json"),
        "finished_at": _now(),
    }))

    # ------------------------------------------------------------------
    # Part 3: READ side - the resume coordinator assembles all three
    # results into one tool-role message and continues the turn.
    # ------------------------------------------------------------------
    parked_row = await session_storage.get(session.id)
    pool = _build_pool(storage_provider)
    fake_executor = _RecordingExecutor()
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(fake_io),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    resume_outcome = await resume_engine_tool_wait(
        pool, _make_lease(session.id), parked_row,
    )

    assert isinstance(resume_outcome, ReleaseOutcome)
    assert resume_outcome.success is True
    assert resume_outcome.drop_lease is False

    assert len(fake_executor.injected) == 1
    injected = fake_executor.injected[0]
    tool_msg = injected[-1]
    assert tool_msg.role == "tool"
    by_id = {p.id: p for p in tool_msg.parts if isinstance(p, ToolCallPart | ToolResultPart)}
    assert set(by_id) == {"x:tool:0:1", "x:tool:0:2", "x:tool:0:3"}
    assert by_id["x:tool:0:1"].output == "result A"
    assert by_id["x:tool:0:2"].output == "result B"
    assert by_id["x:tool:0:3"].output == "notified ok"
