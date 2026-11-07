"""Dormant repair for staged task-scoped ``ask_user`` decisions."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

import primer.claim.task_ask_reconcile as repair_module
from primer.bus.scheduler_tasks import TaskAskDecisionReconciler
from primer.claim.task_ask_reconcile import (
    reconcile_staged_task_asks,
    repair_task_ask_decision,
)
from primer.int.claim import ClaimKind
from primer.model.tool_call_task import (
    TaskGateRepairStage,
    ToolCallTask,
    ToolCallTaskState,
)
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from tests.claim.test_task_ask_resume import (
    NOW,
    TASK_ID,
    _session,
    _stage,
    _stores,
    _task,
)
from tests.conftest import _FakeStorageProvider, _InMemoryStorage


class _Engine:
    def __init__(self) -> None:
        self.leases: set[tuple[ClaimKind, str]] = set()
        self.marks = 0
        self.deletes = 0

    async def mark_resumable(self, kind, entity_id, **kwargs) -> None:
        self.marks += 1
        self.leases.add((kind, entity_id))

    async def has_lease(self, kind, entity_id) -> bool:
        return (kind, entity_id) in self.leases

    async def delete_lease(self, kind, entity_id) -> None:
        self.deletes += 1
        self.leases.discard((kind, entity_id))


class _MutatingEngine(_Engine):
    def __init__(self, sessions, change) -> None:
        super().__init__()
        self._sessions = sessions
        self._change = change

    async def mark_resumable(self, kind, entity_id, **kwargs) -> None:
        await super().mark_resumable(kind, entity_id, **kwargs)
        if kind is ClaimKind.TOOL_CALL:
            session = await self._sessions.get("sess-1")
            await self._sessions.update(session.model_copy(update=self._change))


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "repair.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["reference", "sqlite"])
async def test_receipt_staged_repair_projects_then_promotes_once(
    backend, sqlite_provider,
) -> None:
    tasks, sessions = await _stores(
        sqlite_provider if backend == "sqlite" else None,
    )
    engine = _Engine()
    await _stage(tasks, engine)

    assert await repair_task_ask_decision(
        TASK_ID,
        task_storage=tasks,
        session_storage=sessions,
        engine=engine,
        source="boot",
    ) == "promoted"
    task = await tasks.get(TASK_ID)
    session = await sessions.get("sess-1")
    assert task.state is ToolCallTaskState.QUEUED
    assert task.repair_stage is TaskGateRepairStage.PROMOTED
    assert session.responder_seq == 1
    assert session.turn_person_source_key in session.responder_reservations
    assert engine.leases == {(ClaimKind.TOOL_CALL, TASK_ID)}

    assert await repair_task_ask_decision(
        TASK_ID,
        task_storage=tasks,
        session_storage=sessions,
        engine=engine,
        source="periodic",
    ) == "lease_present"
    assert (await sessions.get("sess-1")).responder_seq == 1
    assert engine.marks == 1


@pytest.mark.asyncio
async def test_projected_crash_repairs_projection_before_promotion() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    staged = await tasks.get(TASK_ID)
    await tasks.update(staged.model_copy(update={
        "repair_stage": TaskGateRepairStage.PROJECTED,
    }))

    assert await repair_task_ask_decision(
        TASK_ID,
        task_storage=tasks,
        session_storage=sessions,
        engine=engine,
        source="periodic",
    ) == "promoted"
    session = await sessions.get("sess-1")
    assert session.responder_seq == 1
    assert session.turn_person_source_key in session.responder_reservations

    engine.leases.clear()
    assert await repair_task_ask_decision(
        TASK_ID,
        task_storage=tasks,
        session_storage=sessions,
        engine=engine,
        source="periodic",
    ) == "rearmed"
    assert engine.marks == 2
    assert (await sessions.get("sess-1")).responder_seq == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_change", "outcome", "task_state", "task_stage"),
    [
        (
            {"status": SessionStatus.PAUSED},
            "deferred",
            ToolCallTaskState.GATED,
            TaskGateRepairStage.PROJECTED,
        ),
        (
            {"cancel_requested": True},
            "quarantined",
            ToolCallTaskState.FAILED,
            TaskGateRepairStage.NONE,
        ),
        (
            {"binding_epoch": 3},
            "quarantined",
            ToolCallTaskState.FAILED,
            TaskGateRepairStage.NONE,
        ),
        (
            {"parked_state": {"kind": "tool_wait", "outstanding_task_ids": []}},
            "quarantined",
            ToolCallTaskState.FAILED,
            TaskGateRepairStage.NONE,
        ),
        (
            {"responder_epoch": "f" * 32},
            "quarantined",
            ToolCallTaskState.FAILED,
            TaskGateRepairStage.NONE,
        ),
    ],
)
async def test_post_promotion_fresh_read_converges_lifecycle_races(
    session_change, outcome, task_state, task_stage,
) -> None:
    tasks, sessions = await _stores()
    engine = _MutatingEngine(sessions, session_change)
    await _stage(tasks, engine)

    assert await repair_task_ask_decision(
        TASK_ID, task_storage=tasks, session_storage=sessions,
        engine=engine, source="periodic",
    ) == outcome
    task = await tasks.get(TASK_ID)
    assert task.state is task_state
    assert task.repair_stage is task_stage
    assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine.leases


@pytest.mark.asyncio
async def test_two_repairers_allocate_one_reservation_and_one_lease() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)

    outcomes = await asyncio.gather(*(
        repair_task_ask_decision(
            TASK_ID,
            task_storage=tasks,
            session_storage=sessions,
            engine=engine,
            source="periodic",
        )
        for _ in range(2)
    ))
    assert set(outcomes) <= {"promoted", "lease_present", "stale"}
    assert "promoted" in outcomes
    session = await sessions.get("sess-1")
    assert session.responder_seq == 1
    assert len(session.responder_reservations) == 1
    assert engine.leases == {(ClaimKind.TOOL_CALL, TASK_ID)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_change", "expected"),
    [
        ({"status": SessionStatus.PAUSED}, "deferred"),
        ({"pause_requested": True, "pause_requested_at": NOW}, "deferred"),
        (
            {"interrupt_requested": True, "interrupt_requested_at": NOW},
            "deferred",
        ),
    ],
)
async def test_transient_lifecycle_withholds_lease_without_mutation(
    session_change, expected,
) -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    before_task = await tasks.get(TASK_ID)
    before_session = await sessions.get("sess-1")
    await sessions.update(before_session.model_copy(update=session_change))
    engine.leases.add((ClaimKind.TOOL_CALL, TASK_ID))

    assert await repair_task_ask_decision(
        TASK_ID,
        task_storage=tasks,
        session_storage=sessions,
        engine=engine,
        source="periodic",
    ) == expected
    assert await tasks.get(TASK_ID) == before_task
    assert (await sessions.get("sess-1")).responder_seq == 0
    assert (ClaimKind.TOOL_CALL, TASK_ID) in engine.leases
    assert engine.deletes == 0


@pytest.mark.asyncio
async def test_promoted_transient_is_held_before_lease_delete() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    assert await repair_task_ask_decision(
        TASK_ID, task_storage=tasks, session_storage=sessions,
        engine=engine, source="boot",
    ) == "promoted"
    session = await sessions.get("sess-1")
    await sessions.update(session.model_copy(update={"status": SessionStatus.PAUSED}))

    assert await repair_task_ask_decision(
        TASK_ID, task_storage=tasks, session_storage=sessions,
        engine=engine, source="periodic",
    ) == "deferred"
    task = await tasks.get(TASK_ID)
    assert task.state is ToolCallTaskState.GATED
    assert task.repair_stage is TaskGateRepairStage.PROJECTED
    assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine.leases
    assert engine.deletes == 1


@pytest.mark.asyncio
async def test_dead_occurrence_quarantines_then_verifies_terminal_siblings(
    monkeypatch,
) -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    session = await sessions.get("sess-1")
    await sessions.update(session.model_copy(update={"binding_epoch": 3}))
    calls: list[str] = []

    async def _verify(**kwargs):
        calls.append(kwargs["session_id"])
        return "stale"

    import primer.claim.tool_wait_reconcile as wake_module

    monkeypatch.setattr(wake_module, "verify_and_mark_batch_resumable", _verify)
    assert await repair_task_ask_decision(
        TASK_ID,
        task_storage=tasks,
        session_storage=sessions,
        engine=engine,
        source="boot",
    ) == "quarantined"
    task = await tasks.get(TASK_ID)
    assert task.state is ToolCallTaskState.FAILED
    assert task.repair_stage is TaskGateRepairStage.NONE
    assert task.gate_state is None
    assert task.result_state["type"] == "tool_result"
    assert task.result_state["id"] == "call-1"
    assert task.result_state["output"] == "task ask decision could not be repaired"
    assert task.result_state["error"] is True
    assert calls == ["sess-1"]
    assert engine.deletes == 1


@pytest.mark.asyncio
async def test_quarantine_runs_real_final_sibling_wake() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    session = await sessions.get("sess-1")
    await sessions.update(session.model_copy(update={
        "binding_epoch": 3,
        "parked_tool_batches": True,
    }))

    assert await repair_task_ask_decision(
        TASK_ID, task_storage=tasks, session_storage=sessions,
        engine=engine, source="boot",
    ) == "quarantined"
    session = await sessions.get("sess-1")
    assert session.parked_status == "resumable"
    assert session.resumable_at is not None
    assert (ClaimKind.SESSION, "sess-1") in engine.leases


class _RefusePatchStorage(_InMemoryStorage[ToolCallTask]):
    async def patch_if(self, *args, **kwargs):
        return None


class _ClaimDuringHoldStorage(_InMemoryStorage[ToolCallTask]):
    async def patch_if(self, id, patch=None, *, where, set_paths=None, conn=None):
        current = self._data[id]
        self._data[id] = current.model_copy(update={
            "state": ToolCallTaskState.RUNNING,
            "claim_token": "worker-token",
        })
        return None


@pytest.mark.asyncio
async def test_quarantine_cas_refusal_preserves_row_and_lease() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    session = await sessions.get("sess-1")
    await sessions.update(session.model_copy(update={"cancel_requested": True}))
    refusing = _RefusePatchStorage(ToolCallTask)
    refusing._data.update(tasks._data)
    engine.leases.add((ClaimKind.TOOL_CALL, TASK_ID))

    assert await repair_task_ask_decision(
        TASK_ID,
        task_storage=refusing,
        session_storage=sessions,
        engine=engine,
        source="periodic",
    ) == "stale"
    assert (await refusing.get(TASK_ID)).state is ToolCallTaskState.GATED
    assert engine.leases == {(ClaimKind.TOOL_CALL, TASK_ID)}
    assert engine.deletes == 0


@pytest.mark.asyncio
async def test_worker_claim_between_hold_read_and_cas_preserves_lease() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    assert await repair_task_ask_decision(
        TASK_ID, task_storage=tasks, session_storage=sessions,
        engine=engine, source="boot",
    ) == "promoted"
    session = await sessions.get("sess-1")
    await sessions.update(session.model_copy(update={"status": SessionStatus.PAUSED}))
    racing = _ClaimDuringHoldStorage(ToolCallTask)
    racing._data.update(tasks._data)

    assert await repair_task_ask_decision(
        TASK_ID, task_storage=racing, session_storage=sessions,
        engine=engine, source="periodic",
    ) == "stale"
    task = await racing.get(TASK_ID)
    assert task.state is ToolCallTaskState.RUNNING
    assert task.claim_token == "worker-token"
    assert engine.leases == {(ClaimKind.TOOL_CALL, TASK_ID)}
    assert engine.deletes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_change",
    [
        {"status": SessionStatus.PAUSED, "cancel_requested": True},
        {
            "status": SessionStatus.ENDED,
            "interrupt_requested": True,
            "interrupt_requested_at": NOW,
        },
    ],
)
async def test_terminal_lifecycle_wins_over_transient_flags(session_change) -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    session = await sessions.get("sess-1")
    await sessions.update(session.model_copy(update=session_change))
    assert await repair_task_ask_decision(
        TASK_ID, task_storage=tasks, session_storage=sessions,
        engine=engine, source="boot",
    ) == "quarantined"
    assert (await tasks.get(TASK_ID)).state is ToolCallTaskState.FAILED


@pytest.mark.asyncio
async def test_correlated_candidate_query_excludes_cross_pairs() -> None:
    provider = _FakeStorageProvider()
    tasks = provider.get_storage(ToolCallTask)
    sessions = provider.get_storage(WorkspaceSession)
    engine = _Engine()
    tasks._data[TASK_ID] = _task()
    sessions._data["sess-1"] = _session()
    await _stage(tasks, engine)
    staged = await tasks.get(TASK_ID)
    tasks._data["wrong-gated"] = staged.model_copy(update={
        "id": "wrong-gated",
        "repair_stage": TaskGateRepairStage.PROMOTED,
    })
    tasks._data["wrong-queued"] = staged.model_copy(update={
        "id": "wrong-queued",
        "state": ToolCallTaskState.QUEUED,
        "repair_stage": TaskGateRepairStage.PROJECTED,
    })

    _cursor, count = await reconcile_staged_task_asks(
        provider, engine, source="boot",
    )
    assert count == 1


@pytest.mark.asyncio
async def test_sqlite_correlated_candidate_query_excludes_cross_pairs(
    sqlite_provider,
) -> None:
    tasks, _sessions = await _stores(sqlite_provider)
    engine = _Engine()
    await _stage(tasks, engine)
    staged = await tasks.get(TASK_ID)
    await tasks.create(staged.model_copy(update={
        "id": "wrong-gated",
        "repair_stage": TaskGateRepairStage.PROMOTED,
    }))
    await tasks.create(staged.model_copy(update={
        "id": "wrong-queued",
        "state": ToolCallTaskState.QUEUED,
        "repair_stage": TaskGateRepairStage.PROJECTED,
    }))
    _cursor, count = await reconcile_staged_task_asks(
        sqlite_provider, engine, source="boot",
    )
    assert count == 1


@pytest.mark.asyncio
async def test_bounded_page_cursor_continues_then_wraps(monkeypatch) -> None:
    provider = _FakeStorageProvider()
    tasks = provider.get_storage(ToolCallTask)
    for index in range(201):
        task = _task().model_copy(update={
            "id": f"candidate-{index:03d}",
            "repair_stage": TaskGateRepairStage.RECEIPT_STAGED,
        })
        tasks._data[task.id] = task
    seen: list[str] = []

    async def _repair(task_id, **kwargs):
        seen.append(task_id)
        return "stale"

    monkeypatch.setattr(repair_module, "repair_task_ask_decision", _repair)
    cursor, count = await reconcile_staged_task_asks(
        provider, _Engine(), source="boot",
    )
    assert count == 200
    assert cursor is not None
    cursor, count = await reconcile_staged_task_asks(
        provider, _Engine(), source="boot", cursor=cursor,
    )
    assert count == 1
    assert cursor is None
    assert len(seen) == 201


@pytest.mark.asyncio
async def test_periodic_reconciler_carries_cursor_and_keeps_it_on_error(
    monkeypatch,
) -> None:
    calls: list[str | None] = []
    wrapped = asyncio.Event()

    async def _reconcile(_provider, _engine, *, source, cursor=None):
        assert source == "periodic"
        calls.append(cursor)
        if len(calls) == 1:
            return "next", 200
        if len(calls) == 2:
            raise RuntimeError("page failed")
        wrapped.set()
        return None, 0

    monkeypatch.setattr(repair_module, "reconcile_staged_task_asks", _reconcile)
    # The scheduler imports lazily from the module; avoid waiting 30 seconds.
    reconciler = TaskAskDecisionReconciler(
        storage_provider=object(), claim_engine=_Engine(), poll_seconds=0,
    )
    reconciler.start()
    await asyncio.wait_for(wrapped.wait(), timeout=1)
    await reconciler.stop()
    assert calls[:3] == [None, "next", "next"]
    assert reconciler._cursor is None


@pytest.mark.asyncio
async def test_periodic_reconciler_carries_cursor_across_leadership_loss(
    monkeypatch,
) -> None:
    calls: list[str | None] = []
    first_tick = asyncio.Event()
    second_tick = asyncio.Event()

    async def _reconcile(_provider, _engine, *, source, cursor=None):
        assert source == "periodic"
        calls.append(cursor)
        (first_tick if len(calls) == 1 else second_tick).set()
        return ("next" if len(calls) == 1 else None), 1

    monkeypatch.setattr(repair_module, "reconcile_staged_task_asks", _reconcile)

    class _Lease:
        def __init__(self) -> None:
            self.lost_event = asyncio.Event()
            self.released = False

        async def release(self) -> None:
            self.released = True

    class _Elector:
        def __init__(self, leases) -> None:
            self.leases = list(leases)

        async def try_acquire(self, role):
            assert role == "task-ask-reconciler"
            return self.leases.pop(0)

    first, second = _Lease(), _Lease()
    reconciler = TaskAskDecisionReconciler(
        storage_provider=object(), claim_engine=_Engine(), poll_seconds=3600,
    )
    reconciler.start(_Elector([first, second]))
    await asyncio.wait_for(first_tick.wait(), timeout=1)
    first.lost_event.set()
    await asyncio.wait_for(second_tick.wait(), timeout=1)
    await reconciler.stop()
    assert calls == [None, "next"]
    assert first.released and second.released
