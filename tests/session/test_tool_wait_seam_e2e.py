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

import asyncio
import json
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
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
    # 01a0518b review: parked_event_key is the FUNCTIONAL wake key (a pure
    # function of session_id/turn_no/the batch's own node segment - "x"
    # here, the chat/workspace surface's node_id=None convention), not
    # ToolWaitPark's own synthetic observability-only event_key.
    assert outcome.park.parked_event_key == "tool_wait:s-tool-wait-1:0:x"

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

    # batch_task_ids is the SAME full-batch list on every row (claimable
    # AND notifying) - what on_release's last-sibling check reads.
    expected_batch = ["x:tool:0:1", "x:tool:0:2", "x:tool:0:3"]
    assert task_a.batch_task_ids == expected_batch
    assert task_b.batch_task_ids == expected_batch
    assert notify_task.batch_task_ids == expected_batch

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


@pytest.mark.asyncio
async def test_crash_retry_replay_with_matching_record_seq_is_a_noop() -> None:
    """01a0518b review: a worker can crash AFTER this turn's ToolCallTask
    rows are created (+ upserted) but BEFORE the ParkRequest is ever
    applied - the lease then simply expires and the turn re-runs from
    session.last_seq unchanged, deterministically re-minting the SAME
    scoped ids and record_seq values (see
    _create_tool_call_task_idempotent's own docstring for the full
    argument). The re-run's create() collides with the crashed attempt's
    own row; this is a genuine replay, not a conflict, and must be
    tolerated as a no-op rather than failing the turn."""
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)

    session = WorkspaceSession(
        id="s-tool-wait-crash-replay",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await session_storage.create(session)

    # Simulate the crashed attempt's own already-durable row: the first
    # claimable call's record_seq is deterministically 1 for a fresh
    # session's first TOOL_CALL append (start_seq=0, "the first record
    # gets start_seq + 1").
    await task_storage.create(ToolCallTask(
        id="x:tool:0:1",
        session_id=session.id,
        turn_no=0,
        tool_name="tool_a",
        state=ToolCallTaskState.QUEUED,
        record_seq=1,
        created_at=_now(),
    ))

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
    outcome = await run_one_session_turn(_make_lease(session.id), deps)

    assert outcome.success is True
    assert outcome.drop_lease is True

    # The pre-existing row survives untouched (no-op, not overwritten);
    # the claim engine still gets an upsert call for it - idempotent
    # registration is always safe to repeat, unlike the row create.
    task_a = await task_storage.get("x:tool:0:1")
    assert task_a.state == ToolCallTaskState.QUEUED
    assert task_a.record_seq == 1
    assert (ClaimKind.TOOL_CALL, "x:tool:0:1") in claim_engine.upserted

    task_b = await task_storage.get("x:tool:0:2")
    assert task_b is not None
    assert task_b.state == ToolCallTaskState.QUEUED


@pytest.mark.asyncio
async def test_crash_retry_with_mismatched_record_seq_fails_loudly() -> None:
    """The mirror case: an existing row with the SAME id but a DIFFERENT
    record_seq is not a valid crash-retry replay (the two attempts did
    not mint the same durable TOOL_CALL record) - something else created
    this id, and that must fail loudly rather than silently resurrect or
    overwrite scheduling state a worker might already be running
    against."""
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)

    session = WorkspaceSession(
        id="s-tool-wait-crash-mismatch",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await session_storage.create(session)

    await task_storage.create(ToolCallTask(
        id="x:tool:0:1",
        session_id=session.id,
        turn_no=0,
        tool_name="tool_a",
        state=ToolCallTaskState.QUEUED,
        record_seq=999,  # deliberately NOT what this turn will compute (1)
        created_at=_now(),
    ))

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
    with pytest.raises(RuntimeError, match="not a crash-retry replay"):
        await run_one_session_turn(_make_lease(session.id), deps)


@pytest.mark.asyncio
async def test_missing_tool_name_fails_loudly_not_unknown() -> None:
    """01a0518b review: a TOOL_CALL record with no name (ToolCallEnd
    fired without a preceding ToolCallStart - malformed adapter output,
    so state.tool_names has no entry) must not silently become
    tool_name="unknown" - that would produce an unexecutable
    ToolCallTask row. It must surface as a missing dict entry so the
    except-ToolWaitPark branch's own ``tool_name is None`` check fails
    loudly instead."""
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)

    session = WorkspaceSession(
        id="s-tool-wait-missing-name",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await session_storage.create(session)

    class _NoStartExecutor:
        async def invoke(self, messages, **kwargs):
            # No preceding ToolCallStart -> scoped_call_ids has no entry
            # either, so the durable record's own id falls back to the
            # RAW provider id ("call_a").
            yield ToolCallEnd(id="call_a", arguments={}, index=0)
            raise ToolWaitPark(
                outstanding_task_ids=["call_a"],
                event_key="tool_wait:call_a",
            )
            yield  # pragma: no cover - generator marker, unreachable

    async def _build_executor(_session: WorkspaceSession):
        return _NoStartExecutor()

    deps = SessionDispatchDeps(
        storage_provider=storage_provider,
        workspace_io=_FakeWorkspaceIO(),
        event_bus=_FakeEventBus(),
        build_executor=_build_executor,
    )
    with pytest.raises(RuntimeError, match="no matching TOOL_CALL record"):
        await run_one_session_turn(_make_lease(session.id), deps)


# ===========================================================================
# Mixed-park wake seam (01a0518b review): the last-sibling re-arm mechanism,
# driven through the REAL ToolCallClaimAdapter.on_release path.
# ===========================================================================


async def _dispatch_and_claim_batch(storage_provider, session_id: str):
    """Dispatch a 2-claimable+1-notifying batch through the tool_wait park
    branch using a REAL claim engine (SessionClaimAdapter +
    ToolCallClaimAdapter, wired with the SAME post-release-hook shape
    ClaimEngineFactory.create wires in production - the adapter itself
    only returns a PostReleaseWake signal; this closure is what actually
    calls durably_mark_session_resumable, strictly after the release's
    own transaction/mutation completes), then claim the two QUEUED
    tasks. Returns ``(engine, session_storage, task_storage, task_leases)``.
    """
    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)

    session = WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await session_storage.create(session)

    notify_result = ToolResultPart(id="x:tool:0:3", output="notified ok", error=False)
    park = ToolWaitPark(
        outstanding_task_ids=["x:tool:0:1", "x:tool:0:2"],
        event_key="tool_wait:x:tool:0:1",
        notifying_results=[("x:tool:0:3", notify_result)],
    )
    fake_io = _FakeWorkspaceIO()
    fake_bus = _FakeEventBus()

    engine = InMemoryClaimEngine(adapters={
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=task_storage),
    })

    async def _wake_session_on_tool_wait_ready(signal) -> None:
        from primer.session.yields import durably_mark_session_resumable

        woken = await session_storage.get(signal.session_id)
        if woken is None:
            return
        await durably_mark_session_resumable(
            woken, event_key=signal.event_key, payload=signal.payload,
            session_storage=session_storage, engine=engine,
        )

    engine.bind_post_release_hook(_wake_session_on_tool_wait_ready)

    async def _build_executor(_session: WorkspaceSession):
        return _ToolWaitExecutor(park)

    deps = SessionDispatchDeps(
        storage_provider=storage_provider,
        workspace_io=fake_io,
        event_bus=fake_bus,
        build_executor=_build_executor,
        claim_engine=engine,
    )
    await engine.upsert(ClaimKind.SESSION, session_id)
    session_lease = next(
        l for l in await engine.claim_due("worker-1", max_count=10)
        if l.entity_id == session_id
    )
    outcome = await run_one_session_turn(session_lease, deps)
    await engine.release(session_lease, outcome=outcome)
    assert outcome.park is not None

    task_leases = await engine.claim_due(
        "worker-1", max_count=10, kinds=[ClaimKind.TOOL_CALL],
    )
    assert len(task_leases) == 2
    return engine, session_storage, task_storage, task_leases


@pytest.mark.asyncio
async def test_last_sibling_on_release_wakes_session_and_resume_continues(
    monkeypatch,
) -> None:
    """01a0518b review, required test: the wake trigger fires through the
    REAL ToolCallClaimAdapter.on_release path - no manual row flipping.
    Releasing the first of two QUEUED siblings must NOT wake the session
    (its sibling is still outstanding); releasing the second (the LAST)
    must flip parked_status to resumable, and resume_engine_tool_wait
    must then correctly route and continue the turn."""
    storage_provider = _FakeStorageProvider()
    engine, session_storage, task_storage, task_leases = (
        await _dispatch_and_claim_batch(storage_provider, "s-wake-1")
    )
    lease_by_id = {lease.entity_id: lease for lease in task_leases}

    task_a = await task_storage.get("x:tool:0:1")
    result_a = ToolResultPart(id="x:tool:0:1", output="result A", error=False)
    await task_storage.update(task_a.model_copy(
        update={"result_state": result_a.model_dump(mode="json")}
    ))
    await engine.release(
        lease_by_id["x:tool:0:1"], outcome=ReleaseOutcome(success=True, drop_lease=True),
    )

    row = await session_storage.get("s-wake-1")
    assert row.parked_status == "parked"  # sibling x:tool:0:2 still QUEUED

    task_b = await task_storage.get("x:tool:0:2")
    result_b = ToolResultPart(id="x:tool:0:2", output="result B", error=False)
    await task_storage.update(task_b.model_copy(
        update={"result_state": result_b.model_dump(mode="json")}
    ))
    await engine.release(
        lease_by_id["x:tool:0:2"], outcome=ReleaseOutcome(success=True, drop_lease=True),
    )

    row = await session_storage.get("s-wake-1")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"tool_wait_ready": True}
    assert row.parked_state["resume_event_key"] == "tool_wait:s-wake-1:0:x"

    pool = _build_pool(storage_provider)
    assert pool._select_resume_handler(row) == pool._resume_engine_tool_wait

    fake_executor = _RecordingExecutor()
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist", lambda _ws_id: _async_return(None),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor", lambda _s, _w: _async_return(fake_executor),
    )

    resume_outcome = await resume_engine_tool_wait(pool, _make_lease(row.id), row)
    assert resume_outcome.success is True
    assert resume_outcome.drop_lease is False
    assert len(fake_executor.injected) == 1
    tool_msg = fake_executor.injected[0][-1]
    by_id = {p.id: p for p in tool_msg.parts if isinstance(p, ToolCallPart | ToolResultPart)}
    assert by_id["x:tool:0:1"].output == "result A"
    assert by_id["x:tool:0:2"].output == "result B"
    assert by_id["x:tool:0:3"].output == "notified ok"


@pytest.mark.asyncio
async def test_concurrent_last_two_siblings_wake_idempotently() -> None:
    """01a0518b review, required test: both on_release calls for the last
    two siblings can observe "all terminal" and both call
    durably_mark_session_resumable - must not crash, and must land on a
    single, correct resumable state regardless of which one's write
    lands last (both compute the SAME event_key + the SAME marker
    payload for this batch, so a lost update between the two concurrent
    writes is harmless - see tool_wait_event_key's own docstring)."""
    storage_provider = _FakeStorageProvider()
    engine, session_storage, task_storage, task_leases = (
        await _dispatch_and_claim_batch(storage_provider, "s-wake-2")
    )
    lease_by_id = {lease.entity_id: lease for lease in task_leases}

    for tid, output in (("x:tool:0:1", "result A"), ("x:tool:0:2", "result B")):
        task = await task_storage.get(tid)
        result = ToolResultPart(id=tid, output=output, error=False)
        await task_storage.update(task.model_copy(
            update={"result_state": result.model_dump(mode="json")}
        ))

    # Both siblings release "at the same time" - each on_release's own
    # sibling-check may observe the OTHER as already terminal or not,
    # exercising the real interleaving rather than a hand-picked order.
    await asyncio.gather(
        engine.release(
            lease_by_id["x:tool:0:1"], outcome=ReleaseOutcome(success=True, drop_lease=True),
        ),
        engine.release(
            lease_by_id["x:tool:0:2"], outcome=ReleaseOutcome(success=True, drop_lease=True),
        ),
    )

    row = await session_storage.get("s-wake-2")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"tool_wait_ready": True}
    assert row.parked_state["resume_event_key"] == "tool_wait:s-wake-2:0:x"
