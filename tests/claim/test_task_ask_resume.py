"""Dormant staged-CAS contract for task-scoped ``ask_user`` decisions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from primer.claim.task_ask_resume import (
    TaskAskResumeOutcome,
    project_task_ask_responder,
    promote_task_ask_resumable,
    stage_task_ask_receipt,
)
from primer.int.claim import ClaimKind
from primer.model.common import dump_for_storage
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.tool_call_task import (
    TaskGateRepairStage,
    ToolCallTask,
    ToolCallTaskState,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded
from primer.session.responder_attribution import (
    ReservationFences,
    project_reservation,
    reserve_canonical_source,
    reserve_source,
)
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.task_gate_runtime import TaskGateState, gate_state_sha256
from tests.conftest import _InMemoryStorage


NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
EPOCH = "e" * 32
TOKEN = "a" * 32
TASK_ID = "sess-1/x:tool:3:1"
EVENT_KEY = "ask_user:sess-1:call-1"
PERSON = PrincipalRef(
    type="user",
    id="person-1",
    display="Person One",
    role="user",
    source="local",
    user_id="person-1",
)
OTHER_PERSON = PrincipalRef(
    type="user",
    id="person-2",
    display="Person Two",
    role="user",
    source="local",
    user_id="person-2",
)


class _Engine:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once

    async def mark_resumable(self, kind, entity_id, **kwargs) -> None:
        assert (kind, entity_id) == (ClaimKind.TOOL_CALL, TASK_ID)
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("lease write failed")


class _RaceSessionStorage(_InMemoryStorage[WorkspaceSession]):
    def __init__(self, moved: dict) -> None:
        super().__init__(WorkspaceSession)
        self.moved = moved
        self.did_move = False

    async def patch_if_or_current(
        self, id, patch=None, *, where, set_paths=None, conn=None,
    ):
        if not self.did_move:
            self.did_move = True
            current = self._data[id]
            await self.update(current.model_copy(update=self.moved))
        return await super().patch_if_or_current(
            id, patch, where=where, set_paths=set_paths, conn=conn,
        )


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _task(**updates) -> ToolCallTask:
    gate = TaskGateState(
        occurrence_token=TOKEN,
        gate_seq=1,
        event_key=EVENT_KEY,
        yielded=Yielded(
            tool_name="ask_user",
            event_key=EVENT_KEY,
            timeout=60,
            resume_metadata={"question": "Colour?"},
        ),
        tool_call_id="call-1",
        tool_arguments={"question": "Colour?"},
        parked_at=NOW,
        deadline=NOW + timedelta(minutes=1),
        binding_epoch=2,
    )
    blob = gate.to_jsonable()
    values = {
        "id": TASK_ID,
        "session_id": "sess-1",
        "turn_no": 3,
        "tool_name": "ask_user",
        "state": ToolCallTaskState.GATED,
        "record_seq": 8,
        "call_id": "call-1",
        "batch_task_ids": [TASK_ID],
        "gate_event_key": EVENT_KEY,
        "gate_until": gate.deadline,
        "gate_occurrence_token": TOKEN,
        "gate_state": blob,
        "gate_state_sha256": gate_state_sha256(blob),
        "gate_seq": 1,
        "created_at": NOW,
        "started_at": NOW,
    }
    values.update(updates)
    return ToolCallTask(**values)


def _session(**updates) -> WorkspaceSession:
    values = {
        "id": "sess-1",
        "workspace_id": "workspace-1",
        "binding": AgentSessionBinding(agent_id="agent-1"),
        "binding_epoch": 2,
        "status": SessionStatus.RUNNING,
        "turn_status": "running",
        "turn_no": 3,
        "last_seq": 8,
        "created_at": NOW,
        "parked_status": "parked",
        "parked_event_key": "tool_wait:sess-1:3:x",
        "parked_at": NOW,
        "parked_state": {
            "kind": "tool_wait",
            "outstanding_task_ids": [TASK_ID],
            "notifying_task_ids": [],
        },
        "responder_seq": 0,
        "responder_epoch": EPOCH,
        "responder_reservation_version": 1,
        "responder_reservations": {},
    }
    values.update(updates)
    return WorkspaceSession(**values)


async def _stores(provider=None):
    if provider is None:
        tasks = _InMemoryStorage(ToolCallTask)
        sessions = _InMemoryStorage(WorkspaceSession)
    else:
        tasks = provider.get_storage(ToolCallTask)
        sessions = provider.get_storage(WorkspaceSession)
    await tasks.create(_task())
    await sessions.create(_session())
    return tasks, sessions


async def _stage(tasks, engine, **updates):
    values = {
        "occurrence_token": TOKEN,
        "gate_seq": 1,
        "event_key": EVENT_KEY,
        "payload": {"response": "blue"},
        "person": PERSON,
        "responder_epoch": EPOCH,
        "accepted_at": NOW + timedelta(seconds=1),
        "task_storage": tasks,
        "engine": engine,
    }
    values.update(updates)
    return await stage_task_ask_receipt(TASK_ID, **values)


def _storage_bytes(row) -> bytes:
    return json.dumps(
        dump_for_storage(row), sort_keys=True, separators=(",", ":"),
    ).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["reference", "sqlite"])
async def test_staged_protocol_orders_receipt_projection_promotion(
    backend, sqlite_provider,
) -> None:
    tasks, sessions = await _stores(
        sqlite_provider if backend == "sqlite" else None,
    )
    engine = _Engine()

    assert await _stage(tasks, engine) is TaskAskResumeOutcome.RECEIPT_STAGED
    staged = await tasks.get(TASK_ID)
    assert staged.state is ToolCallTaskState.GATED
    assert staged.claim_token is None
    assert staged.repair_stage is TaskGateRepairStage.RECEIPT_STAGED
    assert engine.calls == 0

    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.PROJECTED
    projected = await tasks.get(TASK_ID)
    session = await sessions.get("sess-1")
    assert projected.repair_stage is TaskGateRepairStage.PROJECTED
    assert session.turn_person == PERSON
    assert session.turn_person_source_key in session.responder_reservations
    assert session.responder_seq == 1

    assert await promote_task_ask_resumable(
        TASK_ID, task_storage=tasks, engine=engine,
    ) is TaskAskResumeOutcome.PROMOTED
    promoted = await tasks.get(TASK_ID)
    assert promoted.state is ToolCallTaskState.QUEUED
    assert promoted.repair_stage is TaskGateRepairStage.PROMOTED
    assert engine.calls == 1


@pytest.mark.asyncio
async def test_exact_retry_reuses_accepted_at_and_repairs_lease() -> None:
    tasks, sessions = await _stores()
    engine = _Engine(fail_once=True)
    assert await _stage(tasks, engine) is TaskAskResumeOutcome.RECEIPT_STAGED
    first = await tasks.get(TASK_ID)
    accepted_at = first.gate_resolution["responder"]["accepted_at"]
    assert await _stage(
        tasks, engine, accepted_at=NOW + timedelta(days=1),
    ) is TaskAskResumeOutcome.RECEIPT_STAGED
    assert (await tasks.get(TASK_ID)).gate_resolution["responder"]["accepted_at"] == accepted_at

    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.PROJECTED
    before = await sessions.get("sess-1")
    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.PROJECTED
    after = await sessions.get("sess-1")
    assert (after.responder_seq, after.responder_reservation_version) == (
        before.responder_seq, before.responder_reservation_version,
    )

    with pytest.raises(RuntimeError, match="lease write failed"):
        await promote_task_ask_resumable(
            TASK_ID, task_storage=tasks, engine=engine,
        )
    assert (await tasks.get(TASK_ID)).repair_stage is TaskGateRepairStage.PROMOTED
    assert await promote_task_ask_resumable(
        TASK_ID, task_storage=tasks, engine=engine,
    ) is TaskAskResumeOutcome.PROMOTED
    assert engine.calls == 2


@pytest.mark.asyncio
async def test_crash_after_reservation_before_projection_repairs_without_second_sequence() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    task = await tasks.get(TASK_ID)
    responder = task.gate_state["resolution"]["responder"]
    row = await sessions.get("sess-1")
    fences = ReservationFences.from_session(row)
    await reserve_canonical_source(
        sessions,
        "sess-1",
        responder_epoch=responder["responder_epoch"],
        person=PERSON,
        domain=responder["domain"],
        source_identity=responder["source_identity"],
        source_key=responder["source_key"],
        accepted_at=datetime.fromisoformat(responder["accepted_at"]),
        fences=fences,
    )
    interrupted = await sessions.get("sess-1")
    assert interrupted.responder_seq == 1
    assert interrupted.turn_person is None
    assert (await tasks.get(TASK_ID)).repair_stage is TaskGateRepairStage.RECEIPT_STAGED

    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.PROJECTED
    repaired = await sessions.get("sess-1")
    assert repaired.responder_seq == 1
    assert repaired.responder_reservation_version == 2
    assert repaired.turn_person == PERSON


@pytest.mark.asyncio
async def test_concurrent_task_stage_winner_after_projection_is_idempotent(monkeypatch) -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    original = tasks.patch_if_or_current

    async def _projector_wins(id, patch=None, *, where, set_paths=None, conn=None):
        row = await tasks.get(id)
        await tasks.update(row.model_copy(update={
            "repair_stage": TaskGateRepairStage.PROJECTED,
        }))
        return await original(
            id, patch, where=where, set_paths=set_paths, conn=conn,
        )

    monkeypatch.setattr(tasks, "patch_if_or_current", _projector_wins)
    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.PROJECTED
    session = await sessions.get("sess-1")
    assert session.responder_seq == 1
    assert session.turn_person == PERSON
    assert (await tasks.get(TASK_ID)).repair_stage is TaskGateRepairStage.PROJECTED


@pytest.mark.asyncio
async def test_digest_race_after_projection_refuses_task_stage_without_regression(
    monkeypatch,
) -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    original = tasks.patch_if_or_current

    async def _digest_moves(id, patch=None, *, where, set_paths=None, conn=None):
        row = await tasks.get(id)
        await tasks.update(row.model_copy(update={"gate_state_sha256": "b" * 64}))
        return await original(
            id, patch, where=where, set_paths=set_paths, conn=conn,
        )

    monkeypatch.setattr(tasks, "patch_if_or_current", _digest_moves)
    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.REFUSED_CORRUPT
    session = await sessions.get("sess-1")
    task = await tasks.get(TASK_ID)
    assert session.responder_seq == 1
    assert session.turn_person == PERSON
    assert task.repair_stage is TaskGateRepairStage.RECEIPT_STAGED
    assert task.state is ToolCallTaskState.GATED


@pytest.mark.asyncio
async def test_conflicting_person_or_decision_never_replaces_staged_receipt() -> None:
    tasks, _sessions = await _stores()
    engine = _Engine()
    assert await _stage(tasks, engine) is TaskAskResumeOutcome.RECEIPT_STAGED
    original = await tasks.get(TASK_ID)
    assert await _stage(
        tasks, engine, person=OTHER_PERSON,
    ) is TaskAskResumeOutcome.REFUSED_CONFLICT
    assert await _stage(
        tasks, engine, payload={"response": "red"},
    ) is TaskAskResumeOutcome.REFUSED_CONFLICT
    assert await tasks.get(TASK_ID) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["reference", "sqlite"])
async def test_missing_engine_refuses_before_any_staged_mutation(
    backend, sqlite_provider,
) -> None:
    tasks, sessions = await _stores(
        sqlite_provider if backend == "sqlite" else None,
    )
    task_before = await tasks.get(TASK_ID)
    session_before = await sessions.get("sess-1")
    assert task_before is not None
    assert session_before is not None
    task_bytes = _storage_bytes(task_before)
    session_bytes = _storage_bytes(session_before)

    with pytest.raises(ValueError, match="ClaimEngine is required"):
        await _stage(tasks, None)

    task_after = await tasks.get(TASK_ID)
    session_after = await sessions.get("sess-1")
    assert task_after is not None
    assert session_after is not None
    assert _storage_bytes(task_after) == task_bytes
    assert _storage_bytes(session_after) == session_bytes
    assert task_after.gate_resolution is None
    assert task_after.repair_stage is TaskGateRepairStage.NONE
    assert session_after.responder_reservations == {}
    assert session_after.turn_person is None


@pytest.mark.asyncio
async def test_missing_engine_refuses_before_promoted_mutation() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    )
    before = await tasks.get(TASK_ID)
    with pytest.raises(ValueError, match="ClaimEngine is required"):
        await promote_task_ask_resumable(
            TASK_ID, task_storage=tasks, engine=None,
        )
    assert await tasks.get(TASK_ID) == before


@pytest.mark.asyncio
async def test_live_task_claim_refuses_before_session_projection() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    staged = await tasks.get(TASK_ID)
    await tasks.update(staged.model_copy(update={
        "state": ToolCallTaskState.RUNNING,
        "claim_token": "live-claim",
    }))
    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.REFUSED_STALE
    session = await sessions.get("sess-1")
    assert session.responder_seq == 0
    assert session.turn_person is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moved",
    [
        {"binding_epoch": 3},
        {"turn_no": 4},
        {"parked_event_key": "tool_wait:sess-1:3:later"},
        {
            "pause_requested": True,
            "pause_requested_at": NOW + timedelta(seconds=2),
        },
        {
            "cancel_requested": True,
            "cancel_requested_at": NOW + timedelta(seconds=2),
        },
        {"interrupt_requested": True},
        {"responder_epoch": "f" * 32},
    ],
)
async def test_session_occurrence_races_refuse_without_advancing_task(moved) -> None:
    tasks = _InMemoryStorage(ToolCallTask)
    await tasks.create(_task())
    sessions = _RaceSessionStorage(moved)
    await sessions.create(_session())
    engine = _Engine()
    await _stage(tasks, engine)

    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.REFUSED_SESSION
    task = await tasks.get(TASK_ID)
    assert task.repair_stage is TaskGateRepairStage.RECEIPT_STAGED
    assert task.state is ToolCallTaskState.GATED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_move",
    [
        {"gate_state_sha256": "b" * 64},
        {"repair_stage": TaskGateRepairStage.PROJECTED},
        {"gate_seq": 2},
        {"gate_event_key": "ask_user:sess-1:later"},
        {"turn_no": 4},
    ],
)
async def test_task_generation_races_refuse_stage_without_lease(task_move, monkeypatch) -> None:
    tasks, _sessions = await _stores()
    engine = _Engine()
    original = tasks.patch_if_or_current

    async def _move(id, patch=None, *, where, set_paths=None, conn=None):
        row = await tasks.get(id)
        await tasks.update(row.model_copy(update=task_move))
        return await original(
            id, patch, where=where, set_paths=set_paths, conn=conn,
        )

    monkeypatch.setattr(tasks, "patch_if_or_current", _move)
    assert await _stage(tasks, engine) in {
        TaskAskResumeOutcome.REFUSED_STALE,
        TaskAskResumeOutcome.REFUSED_CORRUPT,
    }
    assert (await tasks.get(TASK_ID)).gate_resolution is None


@pytest.mark.asyncio
async def test_classic_then_task_and_task_then_classic_use_total_sequence_order() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    row = await sessions.get("sess-1")
    fences = ReservationFences.from_session(row)
    classic = await reserve_source(
        sessions,
        "sess-1",
        responder_epoch=EPOCH,
        person=OTHER_PERSON,
        domain="ask_user",
        identity={"classic": 1},
        accepted_at=NOW,
        fences=fences,
    )
    await project_reservation(
        sessions,
        "sess-1",
        responder_epoch=EPOCH,
        source_key=classic.source_key,
        fences=fences,
    )
    await _stage(tasks, engine)
    await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    )
    task_projection = await sessions.get("sess-1")
    assert task_projection.turn_person == PERSON
    assert task_projection.turn_person_seq == 2

    latest_fences = ReservationFences.from_session(task_projection)
    later = await reserve_source(
        sessions,
        "sess-1",
        responder_epoch=EPOCH,
        person=OTHER_PERSON,
        domain="ask_user",
        identity={"classic": 2},
        accepted_at=NOW + timedelta(seconds=3),
        fences=latest_fences,
    )
    await project_reservation(
        sessions,
        "sess-1",
        responder_epoch=EPOCH,
        source_key=later.source_key,
        fences=latest_fences,
    )
    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.PROJECTED
    final = await sessions.get("sess-1")
    assert final.turn_person == OTHER_PERSON
    assert final.turn_person_seq == 3


@pytest.mark.asyncio
async def test_missing_or_moved_tool_wait_membership_never_projects() -> None:
    tasks, sessions = await _stores()
    engine = _Engine()
    await _stage(tasks, engine)
    row = await sessions.get("sess-1")
    await sessions.update(row.model_copy(update={
        "parked_state": {
            "kind": "tool_wait",
            "outstanding_task_ids": ["sess-1/x:tool:3:other"],
            "notifying_task_ids": [],
        },
    }))
    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.REFUSED_SESSION
    assert (await sessions.get("sess-1")).responder_seq == 0
