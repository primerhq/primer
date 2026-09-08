"""``resume_graph_tool_wait`` and ``resume_graph_engine``'s per-drain
tool_wait readiness re-check (Phase 3 stage 7a, 01a0518b boundary d) -
direct/coordinator-level tests. The graph order tests
(``tests/graph/test_tool_wait_graph_park.py``) exercise the EXECUTOR's
own pending-list bookkeeping directly; these tests exercise the
WORKER-LAYER coordinators built on top of it, which had no direct
coverage of their own:

* ``resume_graph_tool_wait`` - the pure-path coordinator (no co-pending
  human gate): drains to completion, partially wakes on a two-batch
  fan-out, and fails closed when nothing is actually ready.
* ``resume_graph_engine``'s readiness re-check - the routing-gap fix:
  a co-pending tool_wait batch that is ALREADY terminal at the moment a
  human gate's reply is being drained must resolve in that SAME cycle,
  not wait for an unrelated later reply.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ReleaseOutcome
from primer.model.chat import Message, ToolResultPart
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding, SessionStatus, WorkspaceSession,
)
from primer.model.yield_ import ToolWaitPark, YieldToWorker
from primer.worker import graph_resume_coordinator
from primer.worker.tool_wait_resume_coordinator import resume_graph_tool_wait
from primer.worker.yield_runtime import ParkedState, ToolWaitParkedState

from tests.conftest import _FakeStorageProvider
from tests.graph.test_tool_wait_graph_park import (
    _ask_user_yield,
    _mk_parallel_executor,
    _patch_run_agent_turn,
    _tool_wait_park,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session(session_id: str = "gs-1") -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING, created_at=_now(), turn_no=0, parked_at=_now(),
    )


class _StorageProvider:
    """Routes ToolCallTask through a real _FakeStorageProvider; nothing
    else is touched by these tests."""

    def __init__(self) -> None:
        self._inner = _FakeStorageProvider()

    def get_storage(self, model_cls):
        return self._inner.get_storage(model_cls)


class _NoopClaimEngine:
    """Stands in for WorkerPool._engine - these tests exercise readiness
    / repark routing, not row-creation content, so upserts are discarded."""

    async def upsert(self, kind, entity_id: str, **kwargs) -> None:
        return None


class _FakePool:
    def __init__(self, *, storage, workspace_io, executor_factory) -> None:
        self._storage = storage
        self._workspace_io = workspace_io
        self._event_bus = None
        self._engine = _NoopClaimEngine()
        self._executor_factory = executor_factory
        self.end_session_calls: list[str] = []
        self.repark_calls: list = []

    async def _load_workspace_for_persist(self, workspace_id: str):
        return self._workspace_io

    async def _build_graph_executor(self, session, workspace):
        return await self._executor_factory()

    async def _end_session(self, session, *, reason: str):
        self.end_session_calls.append(reason)
        return f"ENDED:{reason}"

    def _repark_graph_outcome(self, session, repark, *, node_tool_call_seq=None):
        self.repark_calls.append(repark)
        return "REPARKED"

    # -- resume_graph_engine's own delegating surface --------------------
    def _graph_nested_agent_yield(self, checkpoint, tcid):
        return graph_resume_coordinator.graph_nested_agent_yield(self, checkpoint, tcid)

    def _graph_value_yield_toolcall(self, checkpoint, tcid):
        return graph_resume_coordinator.graph_value_yield_toolcall(self, checkpoint, tcid)

    async def _graph_agent_tool_result(self, checkpoint, tcid, payload):
        # Directly supplies the ask_user answer, bypassing the global
        # resume-hook registry - irrelevant to what these tests prove.
        return Message(role="tool", parts=[ToolResultPart(id=tcid, output="blue")])

    async def _write_approval_record_for_graph(self, *, session, checkpoint, tcid, payload):
        return None

    async def _persist_resume_tool_result_record_for_graph(
        self, *, session, checkpoint, tcid, agent_tool_result,
    ):
        return None

    async def _resume_graph_continuation(self, *args, **kwargs):
        raise AssertionError("no nested continuation in these tests")


class _FakeWorkspaceIO:
    async def append_message_line(self, session_id: str, line: bytes) -> None:
        return None


def _make_parked_batch(scoped_id: str, *, state: ToolCallTaskState, output: str) -> ToolCallTask:
    return ToolCallTask(
        id=scoped_id, session_id="gs-1", turn_no=0, tool_name="t",
        state=state, record_seq=1, created_at=_now(),
        result_state=(
            {"id": scoped_id, "output": output, "error": False}
            if state in (ToolCallTaskState.DONE, ToolCallTaskState.FAILED) else None
        ),
    )


# ===========================================================================
# resume_graph_tool_wait - pure path (no co-pending human gate)
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_graph_tool_wait_drains_to_completion(monkeypatch) -> None:
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _tool_wait_park("B", "1"),
    })
    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    graph_checkpoint = excinfo.value.graph_checkpoint

    storage = _StorageProvider()
    task_storage = storage.get_storage(ToolCallTask)
    await task_storage.create(_make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.DONE, output="result A",
    ))
    await task_storage.create(_make_parked_batch(
        "B:tool:0:1", state=ToolCallTaskState.DONE, output="result B",
    ))

    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=_mk_parallel_executor,
    )
    parked = ToolWaitParkedState(
        outstanding_task_ids=["A:tool:0:1", "B:tool:0:1"], notifying_task_ids=[],
        event_key="tool_wait:A:tool:0:1", llm_messages=[], turn_no=0,
        started_at=_now(), graph_checkpoint=graph_checkpoint,
    )

    outcome = await resume_graph_tool_wait(pool, _session(), parked)

    assert outcome == "ENDED:completed"
    assert pool.end_session_calls == ["completed"]
    assert pool.repark_calls == []


@pytest.mark.asyncio
async def test_resume_graph_tool_wait_partial_wake_reparks_on_remaining_node(
    monkeypatch,
) -> None:
    """Only A's batch is terminal; B's is still mid-flight - must resume
    A and re-park on B alone through the FULL worker-layer coordinator,
    not just the executor (proven directly in the graph order tests)."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _tool_wait_park("B", "1"),
    })
    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    graph_checkpoint = excinfo.value.graph_checkpoint

    storage = _StorageProvider()
    task_storage = storage.get_storage(ToolCallTask)
    await task_storage.create(_make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.DONE, output="result A",
    ))
    await task_storage.create(_make_parked_batch(
        "B:tool:0:1", state=ToolCallTaskState.QUEUED, output="",
    ))

    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=_mk_parallel_executor,
    )
    parked = ToolWaitParkedState(
        outstanding_task_ids=["A:tool:0:1", "B:tool:0:1"], notifying_task_ids=[],
        event_key="tool_wait:A:tool:0:1", llm_messages=[], turn_no=0,
        started_at=_now(), graph_checkpoint=graph_checkpoint,
    )

    outcome = await resume_graph_tool_wait(pool, _session(), parked)

    assert outcome == "REPARKED"
    assert pool.end_session_calls == []
    assert len(pool.repark_calls) == 1
    repark = pool.repark_calls[0]
    assert isinstance(repark, ToolWaitPark)
    remaining = repark.graph_checkpoint["pending_tool_waits"]
    assert [pw["node_id"] for pw in remaining] == ["B"]


@pytest.mark.asyncio
async def test_resume_graph_tool_wait_fails_when_nothing_ready(monkeypatch) -> None:
    """A structural surprise: the session woke, but no node's batch is
    actually fully terminal - fail-closed, mirroring the agent-only
    path's own posture for a missing/non-terminal task."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {"agent-a": _tool_wait_park("A", "1")})
    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    graph_checkpoint = excinfo.value.graph_checkpoint

    storage = _StorageProvider()
    task_storage = storage.get_storage(ToolCallTask)
    await task_storage.create(_make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.QUEUED, output="",
    ))

    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=_mk_parallel_executor,
    )
    parked = ToolWaitParkedState(
        outstanding_task_ids=["A:tool:0:1"], notifying_task_ids=[],
        event_key="tool_wait:A:tool:0:1", llm_messages=[], turn_no=0,
        started_at=_now(), graph_checkpoint=graph_checkpoint,
    )

    outcome = await resume_graph_tool_wait(pool, _session(), parked)

    assert outcome == "ENDED:failed"
    assert pool.end_session_calls == ["failed"]


# ===========================================================================
# resume_graph_engine - the routing-gap fix: a co-pending tool_wait batch
# that's ALREADY terminal must resolve in the SAME cycle a human gate's
# reply is drained, not wait for an unrelated later reply.
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_graph_engine_resolves_co_pending_tool_wait_same_cycle(
    monkeypatch,
) -> None:
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _ask_user_yield("B", "tc-b"),
    })
    with pytest.raises(YieldToWorker) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    graph_checkpoint = first_park.graph_checkpoint

    storage = _StorageProvider()
    task_storage = storage.get_storage(ToolCallTask)
    # A's batch is ALREADY fully terminal by the time B's gate is
    # answered - the exact shape the routing gap fix exists for.
    await task_storage.create(_make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.DONE, output="result A",
    ))

    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=_mk_parallel_executor,
    )

    session = _session()
    session.parked_state = {"resume_event_key": "ask_user:B:tc-b"}
    parked = ParkedState(
        yielded=first_park.yielded, llm_messages=[], turn_no=0, started_at=_now(),
        tool_call_id=first_park.tool_call_id,
        resume_event_payload={"decision": "approved"},
        graph_checkpoint=graph_checkpoint,
    )

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    # Both B (the answered gate) AND A (the co-pending tool_wait batch,
    # resolved via the SAME-cycle readiness re-check) drained - the graph
    # reaches completion in one resume, not a repark stranding A.
    assert outcome == "ENDED:completed"
    assert pool.end_session_calls == ["completed"]
    assert pool.repark_calls == []


@pytest.mark.asyncio
async def test_resume_graph_engine_leaves_not_yet_ready_tool_wait_pending(
    monkeypatch,
) -> None:
    """The mirror case: A's batch is NOT yet terminal when B's gate is
    answered - the readiness re-check must not spuriously resolve it;
    the resume re-parks on A alone via the ToolWaitPark shape."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _ask_user_yield("B", "tc-b"),
    })
    with pytest.raises(YieldToWorker) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    graph_checkpoint = first_park.graph_checkpoint

    storage = _StorageProvider()
    task_storage = storage.get_storage(ToolCallTask)
    await task_storage.create(_make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.QUEUED, output="",
    ))

    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=_mk_parallel_executor,
    )

    session = _session()
    session.parked_state = {"resume_event_key": "ask_user:B:tc-b"}
    parked = ParkedState(
        yielded=first_park.yielded, llm_messages=[], turn_no=0, started_at=_now(),
        tool_call_id=first_park.tool_call_id,
        resume_event_payload={"decision": "approved"},
        graph_checkpoint=graph_checkpoint,
    )

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    assert outcome == "REPARKED"
    assert pool.end_session_calls == []
    assert len(pool.repark_calls) == 1
    repark = pool.repark_calls[0]
    assert isinstance(repark, ToolWaitPark)
    remaining = repark.graph_checkpoint["pending_tool_waits"]
    assert [pw["node_id"] for pw in remaining] == ["A"]


# ===========================================================================
# 7a gate verdict item 4: a tool_wait wake's own event_key riding in the
# SAME resume_event_payloads accumulation dict as a human gate's real
# reply must never reach the approval-decision machinery - it fails
# closed to "rejected" on the malformed {"tool_wait_ready": True}
# payload and installs a rejecting bypass override on the SHARED
# executor, silently rejecting a genuinely approved co-pending gate
# processed on a later loop iteration.
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_graph_engine_tool_wait_wake_does_not_reject_approved_gate(
    monkeypatch,
) -> None:
    """Both a tool_wait wake and a real, approved gate reply accumulate
    in the SAME resume_event_payloads dict this cycle - the tool_wait
    entry must be filtered out of the approval loop, not corrupt the
    real reply's own bypass-dispatch handling."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _ask_user_yield("B", "tc-b"),
    })
    with pytest.raises(YieldToWorker) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    graph_checkpoint = first_park.graph_checkpoint

    storage = _StorageProvider()
    task_storage = storage.get_storage(ToolCallTask)
    await task_storage.create(_make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.DONE, output="result A",
    ))

    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=_mk_parallel_executor,
    )

    session = _session()
    # durably_mark_session_resumable's own accumulation shape - the
    # tool_wait wake and the human gate's real reply share ONE dict,
    # keyed by an opaque dispatch_key (never read by the value-shape
    # itself, so arbitrary keys are fine here).
    session.parked_state = {
        "resume_event_payloads": {
            "tool_wait_key": {
                "event_key": "tool_wait:gs-1:0:A",
                "payload": {"tool_wait_ready": True},
            },
            "gate_key": {
                "event_key": "ask_user:B:tc-b",
                "payload": {"decision": "approved"},
            },
        },
    }
    parked = ParkedState(
        yielded=first_park.yielded, llm_messages=[], turn_no=0, started_at=_now(),
        tool_call_id=first_park.tool_call_id,
        resume_event_payload={"decision": "approved"},
        graph_checkpoint=graph_checkpoint,
    )

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    # Both the gate (real reply) and A's tool_wait batch (readiness
    # re-check) drained in this one cycle - the graph completes.
    assert outcome == "ENDED:completed"
    assert pool.end_session_calls == ["completed"]
    assert pool.repark_calls == []


@pytest.mark.asyncio
async def test_resume_graph_engine_tool_wait_only_wake_runs_readiness_recheck(
    monkeypatch,
) -> None:
    """The other half of item 4's fix: when EVERY accumulated reply this
    cycle is a tool_wait wake (no human gate answered yet), filtering
    must not leave `replies` empty and skip the readiness re-check
    entirely - and the still-unanswered gate must survive untouched
    (never spuriously treated as answered by the fallback sentinel)."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {
        "agent-a": _tool_wait_park("A", "1"),
        "agent-b": _ask_user_yield("B", "tc-b"),
    })
    with pytest.raises(YieldToWorker) as excinfo:
        async for _ev in ex.invoke([]):
            pass
    first_park = excinfo.value
    graph_checkpoint = first_park.graph_checkpoint

    storage = _StorageProvider()
    task_storage = storage.get_storage(ToolCallTask)
    await task_storage.create(_make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.DONE, output="result A",
    ))

    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=_mk_parallel_executor,
    )

    session = _session()
    session.parked_state = {
        "resume_event_payloads": {
            "tool_wait_key": {
                "event_key": "tool_wait:gs-1:0:A",
                "payload": {"tool_wait_ready": True},
            },
        },
    }
    parked = ParkedState(
        yielded=first_park.yielded, llm_messages=[], turn_no=0, started_at=_now(),
        tool_call_id=first_park.tool_call_id,
        resume_event_payload={"tool_wait_ready": True},
        graph_checkpoint=graph_checkpoint,
    )

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    # A resolved via the readiness re-check despite no human reply this
    # cycle; B's gate was NEVER answered and survives the re-park.
    assert outcome == "REPARKED"
    assert pool.end_session_calls == []
    assert len(pool.repark_calls) == 1
    repark = pool.repark_calls[0]
    assert isinstance(repark, YieldToWorker)
    remaining_ay = repark.graph_checkpoint["pending_agent_yields"]
    assert [ay["node_id"] for ay in remaining_ay] == ["B"]
    assert repark.graph_checkpoint.get("pending_tool_waits") in (None, [])


# ===========================================================================
# 7a gate verdict item C: persist_resume_tool_result_records must seed its
# writer from a FRESH storage read of last_seq, not the (possibly stale)
# session parameter threaded down the call chain - a prior resume-drain
# tap (_ResumeDrainTap, graph_resume.py) may already have advanced
# last_seq in storage by the time this runs, for both resume_graph_
# tool_wait and resume_graph_engine's own readiness re-check integration.
# ===========================================================================


@pytest.mark.asyncio
async def test_persist_resume_tool_result_records_seeds_from_fresh_last_seq() -> None:
    from primer.worker.tool_wait_resume_coordinator import (
        persist_resume_tool_result_records,
    )

    storage = _StorageProvider()
    session_storage = storage.get_storage(WorkspaceSession)
    stale_session = _session()
    await session_storage.create(stale_session)
    assert stale_session.last_seq == 0

    # Simulate "a prior resume-drain tap already advanced last_seq in
    # storage" AFTER the caller's own `session` snapshot was taken.
    await session_storage.update(
        stale_session.model_copy(update={"last_seq": 10})
    )

    task = _make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.DONE, output="result A",
    )
    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=lambda: None,
    )

    # `stale_session` (last_seq=0) is what a caller holding an
    # earlier-read snapshot would pass - the fresh row in storage is
    # already at 10.
    await persist_resume_tool_result_records(pool, stale_session, [task])

    updated = await session_storage.get(stale_session.id)
    # Seeded from 10 (fresh), not 0 (stale) - exactly one record
    # appended, so last_seq lands at 11, never colliding with or
    # repeating whatever the prior drain already wrote up to 10.
    assert updated.last_seq == 11


@pytest.mark.asyncio
async def test_persist_resume_tool_result_records_preserves_other_fresh_fields() -> None:
    """The final storage.update must not roll back OTHER fields a prior
    drain already wrote (e.g. status) by model_copy-ing from the stale
    session snapshot instead of the fresh row."""
    from primer.worker.tool_wait_resume_coordinator import (
        persist_resume_tool_result_records,
    )

    storage = _StorageProvider()
    session_storage = storage.get_storage(WorkspaceSession)
    stale_session = _session()
    await session_storage.create(stale_session)

    await session_storage.update(stale_session.model_copy(update={
        "last_seq": 10, "status": SessionStatus.ENDED,
    }))

    task = _make_parked_batch(
        "A:tool:0:1", state=ToolCallTaskState.DONE, output="result A",
    )
    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(),
        executor_factory=lambda: None,
    )

    await persist_resume_tool_result_records(pool, stale_session, [task])

    updated = await session_storage.get(stale_session.id)
    assert updated.status == SessionStatus.ENDED
