"""A binding switch queued on a parked session is applied when its resume FAILS and the engine path ends the session.

A switch queued while a turn runs (the route, or the agent's own ``switch_binding`` tool) sits in
``pending_binding_switch`` until the turn's drain checkpoint applies it. A turn that parks on a ``tool_wait`` batch
(or any other park) is continued by the resume, and the continuation turn reaches that checkpoint. When the resume
takes one of its failure exits instead, every exit is ``pool._end_session(...)``: no checkpoint runs, the switch
survives on the ENDED row, and after a later steer reopens the session the user's next message is answered by the
OUTGOING binding (the switch applies only at that turn's own checkpoint). Every terminal exit of ``dispatch.py``
applies the switch (the failed turn, the completed turn, the cancelled turn); the pool's ``_end_session`` did not.

Driven through the REAL ``WorkerPool._end_session`` and the real resume coordinators' failure exits (nothing about
the end is stubbed); the workspace is an in-memory ``messages.jsonl`` behind the real io shim.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import primer.worker.tool_wait_resume_coordinator as tool_wait_coordinator
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Message, ToolCallPart
from primer.model.scheduler import WorkerConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.worker.pool import WorkerPool
from primer.worker.session_resume_coordinator import resume_engine_session
from primer.worker.yield_runtime import ToolWaitParkedState

from tests.conftest import _FakeStorageProvider

T0 = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
SID = "s-switch"
TASK_A, TASK_B = f"{SID}/x:tool:0:1", f"{SID}/x:tool:0:2"
SWITCH = {"kind": "agent", "agent_id": "ag-2", "graph_id": None, "profile_id": None, "actor": "user"}


class _Workspace:
    """The one method the io shim calls: ``messages.jsonl`` appends, kept in memory."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append(line)

    async def read_file(self, path: str) -> bytes:
        """What every real workspace backend has: the switch reads the log whole to find an orphan marker."""
        if not self.lines:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")  # both real backends raise this for an absent file
        return b"".join(self.lines)

    def records(self) -> list[dict]:
        return [json.loads(ln) for ln in self.lines]


class _Registry:
    def __init__(self, workspace: _Workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str) -> _Workspace:
        return self._workspace


def _lease() -> Lease:
    return Lease(
        kind=ClaimKind.SESSION, entity_id=SID, claimed_by="wrk", claimed_at=T0, expires_at=T0,
        attempt_count=1, last_error=None,
    )


def _task(task_id: str, state: ToolCallTaskState) -> ToolCallTask:
    return ToolCallTask(
        id=task_id, session_id=SID, turn_no=0, tool_name="t", state=state, record_seq=1,
        created_at=T0, finished_at=T0 if state is ToolCallTaskState.DONE else None,
        result_state={"type": "tool_result", "id": task_id, "output": "ok", "error": False}
        if state is ToolCallTaskState.DONE else None,
    )


def _tool_wait_session(*, with_switch: bool = True) -> WorkspaceSession:
    parked = ToolWaitParkedState(
        outstanding_task_ids=[TASK_A, TASK_B], notifying_task_ids=[], event_key=f"tool_wait:{SID}:0:x",
        llm_messages=[Message(role="assistant", parts=[
            ToolCallPart(id=t, name="t", arguments={}) for t in (TASK_A, TASK_B)
        ]).model_dump(mode="json")],
        turn_no=0, started_at=T0,
    )
    return WorkspaceSession(
        id=SID, workspace_id="w1", binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.WAITING, turn_status="idle", parked_status="resumable",
        parked_state=parked.to_jsonable(), created_at=T0,
        pending_binding_switch=dict(SWITCH) if with_switch else None,
    )


class _World:
    def __init__(self, session: WorkspaceSession, *tasks: ToolCallTask) -> None:
        self.provider = _FakeStorageProvider()
        self.sessions = self.provider.get_storage(WorkspaceSession)
        self.workspace = _Workspace()
        self.session = session
        self.tasks = tasks
        self.pool = WorkerPool(
            config=WorkerConfig(concurrency=1), scheduler=None, storage=self.provider,  # type: ignore[arg-type]
            workspace_registry=_Registry(self.workspace), provider_registry=None, engine=None, event_bus=None,  # type: ignore[arg-type]
        )

    async def seed(self) -> None:
        await self.sessions.create(self.session)
        task_storage = self.provider.get_storage(ToolCallTask)
        for task in self.tasks:
            await task_storage.create(task)

    async def row(self) -> WorkspaceSession:
        row = await self.sessions.get(SID)
        assert row is not None
        return row

    def markers(self) -> list[dict]:
        return [r for r in self.workspace.records() if r["kind"] == SessionMessageKind.AGENT_MARKER.value]


async def _assert_ended_failed_with_the_switch_applied(world: _World) -> None:
    row = await world.row()
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed"), "the resume did not take a failure exit"
    assert row.pending_binding_switch is None, (
        f"the queued switch survived the failed resume: {row.pending_binding_switch} "
        f"(the next steer would be answered by {row.binding.agent_id!r})"
    )
    assert row.binding.agent_id == "ag-2" and row.binding_epoch == 1
    (marker,) = world.markers()
    assert marker["payload"]["actor"] == "user" and marker["payload"]["binding_epoch"] == 1


@pytest.mark.asyncio
async def test_a_tool_wait_resume_that_finds_a_task_row_missing_applies_the_queued_switch():
    world = _World(_tool_wait_session(), _task(TASK_A, ToolCallTaskState.DONE))  # TASK_B has no row
    await world.seed()

    outcome = await tool_wait_coordinator.resume_engine_tool_wait(world.pool, _lease(), world.session)

    assert outcome.success is True and outcome.drop_lease is True
    await _assert_ended_failed_with_the_switch_applied(world)


@pytest.mark.asyncio
async def test_a_tool_wait_resume_that_finds_a_task_not_terminal_applies_the_queued_switch():
    world = _World(_tool_wait_session(), _task(TASK_A, ToolCallTaskState.DONE), _task(TASK_B, ToolCallTaskState.QUEUED))
    await world.seed()

    await tool_wait_coordinator.resume_engine_tool_wait(world.pool, _lease(), world.session)

    await _assert_ended_failed_with_the_switch_applied(world)


@pytest.mark.asyncio
async def test_a_tool_wait_resume_whose_persist_fails_applies_the_queued_switch(monkeypatch):
    world = _World(_tool_wait_session(), _task(TASK_A, ToolCallTaskState.DONE), _task(TASK_B, ToolCallTaskState.DONE))
    await world.seed()

    class _FailingExecutor:
        async def inject_resume_messages(self, messages):
            raise OSError("the workspace volume is not answering")

    async def load_workspace(_workspace_id):
        return None

    async def build_executor(_session, _workspace):
        return _FailingExecutor()

    monkeypatch.setattr(world.pool, "_load_workspace_for_persist", load_workspace)
    monkeypatch.setattr(world.pool, "_build_agent_executor", build_executor)

    await tool_wait_coordinator.resume_engine_tool_wait(world.pool, _lease(), world.session)

    await _assert_ended_failed_with_the_switch_applied(world)


@pytest.mark.asyncio
async def test_a_human_gate_resume_that_fails_closed_applies_the_queued_switch():
    """The funnel is ``_end_session``, not the tool_wait coordinator: an ``ask_user`` park whose blob is unreadable."""
    session = _tool_wait_session().model_copy(update={"parked_state": {"garbage": True}})
    world = _World(session)
    await world.seed()

    await resume_engine_session(world.pool, _lease(), world.session)

    await _assert_ended_failed_with_the_switch_applied(world)


@pytest.mark.asyncio
async def test_a_failed_resume_with_no_queued_switch_writes_nothing_extra():
    world = _World(_tool_wait_session(with_switch=False), _task(TASK_A, ToolCallTaskState.DONE))
    await world.seed()

    await tool_wait_coordinator.resume_engine_tool_wait(world.pool, _lease(), world.session)

    row = await world.row()
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.binding.agent_id == "ag-1" and row.binding_epoch == 0
    assert world.workspace.lines == [], "an end with nothing queued must not write a record"


@pytest.mark.asyncio
async def test_a_switch_that_cannot_be_applied_does_not_fail_the_end(monkeypatch):
    """Best effort, like every checkpoint: the end is the row's truth and must land even when the apply raises."""
    world = _World(_tool_wait_session(), _task(TASK_A, ToolCallTaskState.DONE))
    await world.seed()

    async def broken_append(session_id: str, line: bytes) -> None:
        raise OSError("the workspace volume is not answering")

    monkeypatch.setattr(world.workspace, "append_message_line", broken_append)

    outcome = await tool_wait_coordinator.resume_engine_tool_wait(world.pool, _lease(), world.session)

    assert outcome.success is True and outcome.drop_lease is True
    row = await world.row()
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.pending_binding_switch == SWITCH, "a switch that could not be applied stays queued for the next checkpoint"
