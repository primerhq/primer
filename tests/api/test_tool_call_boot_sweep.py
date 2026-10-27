"""The boot sweep of orphaned ToolCallTask rows (BPF wave 1, the executor's step 9).

The sweep runs in the lifespan recovery phase, before the pool starts, and
voids (terminal FAILED + delete lease) every non-terminal row the shared
liveness rule (``task_is_live_for_session``) refuses to keep: the rows of an
ENDED or cancelled session, a park that no longer references them (the
non-byte-identical crash-retry orphan), a turn the session has moved past,
and the session-missing case. A row the rule keeps - a live park references
it, or it belongs to a running session's current turn (the byte-identical
crash-retry replay whose park has not applied yet) - is left exactly as-is,
lease included.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.api._app_lifespan_phases import sweep_orphaned_tool_call_tasks
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import CLAIM_PRIORITY_RESUME, ClaimKind
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.agent import Agent, AgentModel
from primer.session.yields import tool_wait_event_key
from primer.worker.yield_runtime import ToolWaitParkedState
from tests.conftest import _InMemoryStorage

SESSION_ID = "sess-sweep"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session(
    storage: _InMemoryStorage,
    *,
    status: SessionStatus,
    parked_state: dict | None = None,
    turn_no: int = 0,
    cancel_requested: bool = False,
) -> None:
    row = WorkspaceSession(
        id=SESSION_ID,
        workspace_id="ws-1",
        binding=AgentSessionBinding(
            agent_id="agent-1",
            agent_snapshot=Agent(
                model=AgentModel(profile_id="profile-1"),
                description="sweep session agent",
                tools=[],
            ),
        ),
        binding_epoch=1,
        status=status,
        created_at=_now(),
        turn_no=turn_no,
        cancel_requested=cancel_requested,
        parked_status="parked" if parked_state is not None else None,
        parked_state=parked_state,
        parked_event_key=(
            tool_wait_event_key(SESSION_ID, scoped_task_id=list(parked_state["outstanding_task_ids"])[0])
            if parked_state is not None else None
        ),
        parked_at=_now() if parked_state is not None else None,
    )
    storage._data[row.id] = row  # noqa: SLF001


def _tool_wait_park(task_ids: list[str]) -> dict:
    return ToolWaitParkedState(
        outstanding_task_ids=task_ids,
        notifying_task_ids=[],
        event_key=tool_wait_event_key(SESSION_ID, scoped_task_id=task_ids[0]),
        llm_messages=[],
        turn_no=0,
        started_at=_now(),
    ).to_jsonable()


def _task(storage: _InMemoryStorage, task_id: str, **extra) -> ToolCallTask:
    row = ToolCallTask(
        id=task_id,
        session_id=SESSION_ID,
        turn_no=extra.pop("turn_no", 0),
        tool_name="workspace__write",
        state=extra.pop("state", ToolCallTaskState.QUEUED),
        record_seq=1,
        created_at=_now(),
        **extra,
    )
    storage._data[row.id] = row  # noqa: SLF001
    return row


class _FakeProvider:
    def __init__(self) -> None:
        self._by_cls: dict = {}

    def get_storage(self, model_cls):
        return self._by_cls.setdefault(model_cls, _InMemoryStorage(model_cls))


def _provider_for(task_storage: _InMemoryStorage, session_storage: _InMemoryStorage) -> _FakeProvider:
    provider = _FakeProvider()
    provider._by_cls[ToolCallTask] = task_storage  # noqa: SLF001
    provider._by_cls[WorkspaceSession] = session_storage  # noqa: SLF001
    return provider


# ---- the void cases ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_of_an_ended_session_is_voided_and_its_lease_deleted():
    task_storage = _InMemoryStorage(ToolCallTask)
    session_storage = _InMemoryStorage(WorkspaceSession)
    _session(session_storage, status=SessionStatus.ENDED)
    _task(task_storage, f"{SESSION_ID}/x:tool:0:1")
    provider = _provider_for(task_storage, session_storage)
    engine = InMemoryClaimEngine(adapters={})
    task_id = f"{SESSION_ID}/x:tool:0:1"
    await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    await sweep_orphaned_tool_call_tasks(engine, provider)
    task = task_storage._data[task_id]  # noqa: SLF001
    assert task.state is ToolCallTaskState.FAILED
    assert task.last_error
    assert task.finished_at is not None
    assert (ClaimKind.TOOL_CALL, task_id) not in engine._leases


@pytest.mark.asyncio
async def test_a_row_missing_its_session_row_is_voided():
    task_storage = _InMemoryStorage(ToolCallTask)
    session_storage = _InMemoryStorage(WorkspaceSession)  # no session row at all
    _task(task_storage, f"{SESSION_ID}/x:tool:0:1")
    provider = _provider_for(task_storage, session_storage)
    engine = InMemoryClaimEngine(adapters={})
    task_id = f"{SESSION_ID}/x:tool:0:1"
    await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    await sweep_orphaned_tool_call_tasks(engine, provider)
    assert task_storage._data[task_id].state is ToolCallTaskState.FAILED  # noqa: SLF001
    assert (ClaimKind.TOOL_CALL, task_id) not in engine._leases


@pytest.mark.asyncio
async def test_a_row_not_referenced_by_the_live_park_is_voided():
    task_storage = _InMemoryStorage(ToolCallTask)
    session_storage = _InMemoryStorage(WorkspaceSession)
    # The session parked on a LATER turn's batch: the old task is not in it.
    _session(session_storage, status=SessionStatus.RUNNING,
             parked_state=_tool_wait_park([f"{SESSION_ID}/x:tool:9:1"]))
    _task(task_storage, f"{SESSION_ID}/x:tool:0:1")
    provider = _provider_for(task_storage, session_storage)
    engine = InMemoryClaimEngine(adapters={})
    task_id = f"{SESSION_ID}/x:tool:0:1"
    await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    await sweep_orphaned_tool_call_tasks(engine, provider)
    assert task_storage._data[task_id].state is ToolCallTaskState.FAILED  # noqa: SLF001
    assert (ClaimKind.TOOL_CALL, task_id) not in engine._leases


@pytest.mark.asyncio
async def test_a_row_from_a_turn_the_session_has_moved_past_is_voided():
    task_storage = _InMemoryStorage(ToolCallTask)
    session_storage = _InMemoryStorage(WorkspaceSession)
    # No park at all (a crashed or failed turn), session now on a later turn.
    _session(session_storage, status=SessionStatus.RUNNING, turn_no=3)
    _task(task_storage, f"{SESSION_ID}/x:tool:0:1", turn_no=0)
    provider = _provider_for(task_storage, session_storage)
    engine = InMemoryClaimEngine(adapters={})
    task_id = f"{SESSION_ID}/x:tool:0:1"
    await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    await sweep_orphaned_tool_call_tasks(engine, provider)
    assert task_storage._data[task_id].state is ToolCallTaskState.FAILED  # noqa: SLF001
    assert (ClaimKind.TOOL_CALL, task_id) not in engine._leases


@pytest.mark.asyncio
async def test_a_cancelled_session_voids_its_rows():
    task_storage = _InMemoryStorage(ToolCallTask)
    session_storage = _InMemoryStorage(WorkspaceSession)
    _session(session_storage, status=SessionStatus.RUNNING, cancel_requested=True)
    _task(task_storage, f"{SESSION_ID}/x:tool:0:1")
    provider = _provider_for(task_storage, session_storage)
    engine = InMemoryClaimEngine(adapters={})
    task_id = f"{SESSION_ID}/x:tool:0:1"
    await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    await sweep_orphaned_tool_call_tasks(engine, provider)
    assert task_storage._data[task_id].state is ToolCallTaskState.FAILED  # noqa: SLF001
    assert (ClaimKind.TOOL_CALL, task_id) not in engine._leases


@pytest.mark.asyncio
async def test_a_gated_row_of_an_ended_session_is_voided():
    task_storage = _InMemoryStorage(ToolCallTask)
    session_storage = _InMemoryStorage(WorkspaceSession)
    _session(session_storage, status=SessionStatus.ENDED)
    _task(
        task_storage, f"{SESSION_ID}/x:tool:0:1",
        state=ToolCallTaskState.GATED,
        gate_event_key="tool_approval:sess-sweep:call-1",
    )
    provider = _provider_for(task_storage, session_storage)
    engine = InMemoryClaimEngine(adapters={})
    task_id = f"{SESSION_ID}/x:tool:0:1"
    await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    await sweep_orphaned_tool_call_tasks(engine, provider)
    task = task_storage._data[task_id]  # noqa: SLF001
    assert task.state is ToolCallTaskState.FAILED
    assert task.gate_state is None  # the void clears the gate payload


# ---- the keep cases ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_referenced_by_the_live_park_is_kept_with_its_lease():
    task_storage = _InMemoryStorage(ToolCallTask)
    session_storage = _InMemoryStorage(WorkspaceSession)
    task_id = f"{SESSION_ID}/x:tool:0:1"
    _session(session_storage, status=SessionStatus.RUNNING,
             parked_state=_tool_wait_park([task_id]))
    _task(task_storage, task_id)
    provider = _provider_for(task_storage, session_storage)
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    await sweep_orphaned_tool_call_tasks(engine, provider)
    assert task_storage._data[task_id].state is ToolCallTaskState.QUEUED  # noqa: SLF001
    assert (ClaimKind.TOOL_CALL, task_id) in engine._leases, "the live row keeps its lease"


@pytest.mark.asyncio
async def test_a_current_turn_row_of_an_unparked_running_session_is_kept():
    """The byte-identical crash-retry window: the session crashed mid-turn
    (no park applied yet) and the replay will re-park the same ids; the row
    must survive the boot."""
    task_storage = _InMemoryStorage(ToolCallTask)
    session_storage = _InMemoryStorage(WorkspaceSession)
    task_id = f"{SESSION_ID}/x:tool:0:1"
    _session(session_storage, status=SessionStatus.RUNNING, turn_no=0)
    _task(task_storage, task_id, turn_no=0)
    provider = _provider_for(task_storage, session_storage)
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.TOOL_CALL, task_id, priority=CLAIM_PRIORITY_RESUME)
    await sweep_orphaned_tool_call_tasks(engine, provider)
    assert task_storage._data[task_id].state is ToolCallTaskState.QUEUED  # noqa: SLF001
    assert (ClaimKind.TOOL_CALL, task_id) in engine._leases


def test_the_lifespan_runs_the_sweep_before_the_pool_starts():
    """The wiring pin: the sweep call sits inside the recovery block (the
    claim_engine guard) and strictly before the pool starts, so no claimed
    task is ever an orphan and the first claim cycle sees a clean table."""
    import inspect

    from primer.api import _app_lifespan

    src = inspect.getsource(_app_lifespan)
    sweep_call = src.index("await sweep_orphaned_tool_call_tasks(")
    pool_start = src.index("await worker_pool.start()")
    recover_call = src.index("await recover_sessions(")
    assert recover_call < sweep_call < pool_start
