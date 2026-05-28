"""Tests for the WorkerPool resume branch — the glue that closes
roadmap §7 (yielding-tools worker resume).

The resume hooks + ParkedState round-trip + scheduler claim query +
mark_resumable + clear_park are all unit-tested in isolation in
``test_yield_park_resume.py`` / ``test_yield_runtime.py`` /
``test_approval_resume.py``. This file tests the **dispatch** —
the branch in ``WorkerPool._run_one_turn`` that detects
``session.parked_status == "resumable"`` and drives the resume
end-to-end:

* Builds an agent executor for the parked session.
* Looks up the right resume hook (``sleep``, ``ask_user``,
  ``watch_files``, ``mcp_task``) OR special-cases ``_approval``.
* Awaits the hook with the classified payload.
* Persists [rehydrated_assistant_msg, synthesised_tool_result_msg]
  via the executor's ``inject_resume_messages``.
* Calls ``scheduler.clear_park``.
* Calls ``scheduler.complete_turn(RUNNING, re_enqueue=True)`` so the
  next normal claim drives the continuation LLM turn.

Tests use ``monkeypatch`` to inject a fake workspace + executor +
tool_manager so the dispatch logic can be exercised without a real
LM Studio / Postgres / on-disk workspace.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from matrix.model.chat import Message, ToolCallPart, ToolResultPart
from matrix.model.scheduler import WorkerConfig
from matrix.model.workspace_session import AgentSessionBinding, WorkspaceSession, SessionStatus
from matrix.model.yield_ import Yielded
from matrix.claim.in_memory import InMemoryClaimEngine
from matrix.int.scheduler import Lease as SchedLease
from matrix.scheduler.in_memory import InMemoryScheduler, _LeaseState
from matrix.worker.pool import WorkerPool


def _make_sched_lease(session_id: str, worker_id: str, turn_no: int = 0) -> SchedLease:
    return SchedLease(
        session_id=session_id,
        worker_id=worker_id,
        expires_at=datetime.now(timezone.utc),
        attempt_count=0,
        turn_no=turn_no,
    )
from matrix.worker.yield_runtime import ParkedState

# Import the misc toolset for its resume-hook side-effects
# (register_resume_hook("sleep", ...) at module load time). Without
# this the worker's resume branch raises ConfigError("no resume hook
# registered for 'sleep'") under test, because the toolset module
# wouldn't otherwise be imported in this test process.
import matrix.toolset.misc  # noqa: F401


@pytest.fixture
async def scheduler():
    s = InMemoryScheduler()
    await s.initialize()
    yield s
    await s.aclose()


async def _async_return(value):
    return value


class _RecordingExecutor:
    """Stand-in for WorkspaceAgentExecutor exposing the resume-path
    surface (``inject_resume_messages``) plus a private
    ``_tool_manager`` slot for the _approval-resume special case."""

    def __init__(self, *, tool_manager=None):
        self._tool_manager = tool_manager
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages):
        # Mirror the real method: store the messages so the test can
        # assert on the [assistant_with_tool_use, tool_result] pair.
        self.injected.append(list(messages))


class _NoopPersist:
    async def persist_turn(self, turn_no):
        return None


def _build_pool(scheduler) -> WorkerPool:
    return WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=None,                   # type: ignore[arg-type]
        workspace_registry=None,        # type: ignore[arg-type]
        provider_registry=None,         # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )


def _make_resumable_session(
    sid: str,
    *,
    tool_name: str,
    tool_call_id: str,
    resume_event_payload: dict,
    resume_metadata: dict | None = None,
    llm_messages: list | None = None,
    turn_no: int = 0,
) -> WorkspaceSession:
    """Build a WorkspaceSession row pre-stamped with a resumable park.

    Mirrors what the scheduler's mark_resumable + park_turn leave
    behind: parked_status='resumable', parked_state populated, and
    parked_state.resume_event_payload non-None.
    """
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    yielded = Yielded(
        tool_name=tool_name,
        event_key=f"timer:{tool_call_id}",
        timeout=60.0,
        resume_metadata=dict(resume_metadata or {}),
    )
    # Stamp parked_at_iso as the real worker does in _handle_yield.
    stamped_rm = {**yielded.resume_metadata, "parked_at_iso": parked_at.isoformat()}
    yielded_stamped = Yielded(
        tool_name=yielded.tool_name,
        event_key=yielded.event_key,
        timeout=yielded.timeout,
        resume_metadata=stamped_rm,
    )
    parked_state = ParkedState(
        yielded=yielded_stamped,
        llm_messages=llm_messages or [],
        turn_no=turn_no,
        started_at=parked_at,
        tool_call_id=tool_call_id,
        resume_event_payload=dict(resume_event_payload),
    )

    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=turn_no,
        parked_status="resumable",
        parked_event_key=yielded.event_key,
        parked_until=parked_at + timedelta(seconds=60),
        parked_at=parked_at,
        parked_state=parked_state.to_jsonable(),
    )


# ===========================================================================
# Sleep — the canonical low-overhead resume path
# ===========================================================================


async def test_resume_branch_drives_sleep_hook_end_to_end(
    scheduler, monkeypatch,
):
    """A resumable session parked on the sleep tool: the resume
    branch must look up sleep_resume, call it, persist the
    [assistant_with_tool_use, tool_result] pair via the executor,
    clear the parked columns, and complete_turn(RUNNING, re_enqueue).
    """
    sid = "sess-resume-sleep"
    tool_call_id = "tc-sleep-1"

    # Rehydratable in-flight assistant message: the LLM emitted a
    # tool_use for sleep, the executor stamped it onto YieldToWorker,
    # _handle_yield model_dump'd it into ParkedState.llm_messages.
    assistant_msg = Message(
        role="assistant",
        parts=[
            ToolCallPart(
                id=tool_call_id,
                name="_misc__sleep",
                arguments={"seconds": 5},
            ),
        ],
    )
    llm_messages_dicts = [assistant_msg.model_dump(mode="json")]

    fake_session = _make_resumable_session(
        sid,
        tool_name="sleep",
        tool_call_id=tool_call_id,
        resume_event_payload={},  # timer fired with empty payload
        resume_metadata={"requested_seconds": 5.0},
        llm_messages=llm_messages_dicts,
    )

    # Register the row + lease in the scheduler so claim() can grant
    # us a lease against this session.
    scheduler.register_session_for_test(sid, status=SessionStatus.RUNNING)
    scheduler._sessions[sid] = fake_session   # type: ignore[attr-defined]

    pool = _build_pool(scheduler)
    pool._worker_id = "wrk-resume"
    await scheduler.register_worker(
        worker_id="wrk-resume", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-resume", runnable=True)
    lease = _make_sched_lease(sid, "wrk-resume")

    fake_executor = _RecordingExecutor()
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    await pool._run_one_turn(lease)

    # 1. The resume branch invoked the resume hook and persisted the
    # [assistant_with_tool_use, tool_result] pair.
    assert len(fake_executor.injected) == 1
    injected = fake_executor.injected[0]
    assert len(injected) == 2, (
        f"expected exactly [assistant_with_tool_use, tool_result]; "
        f"got {injected!r}"
    )
    assert injected[0].role == "assistant"
    assistant_part = next(
        p for p in injected[0].parts if isinstance(p, ToolCallPart)
    )
    assert assistant_part.id == tool_call_id
    assert injected[1].role == "tool"
    tool_part = next(
        p for p in injected[1].parts if isinstance(p, ToolResultPart)
    )
    assert tool_part.id == tool_call_id
    # sleep_resume returns a JSON body with requested_seconds +
    # elapsed_seconds; the worker wraps it as ToolResultPart.output.
    body = json.loads(tool_part.output)
    assert body["requested_seconds"] == 5.0
    assert body["elapsed_seconds"] >= 0

    # 2. clear_park ran — parked columns now None.
    assert fake_session.parked_status is None
    assert fake_session.parked_state is None
    assert fake_session.parked_event_key is None

    # 3. complete_turn ran with RUNNING + re-enqueue — the row's
    # turn_no advanced and the lease is runnable again.
    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.turn_no == lease.turn_no + 1
    assert snapshot.status == SessionStatus.RUNNING
    assert scheduler._leases[sid].runnable is True   # type: ignore[attr-defined]


# ===========================================================================
# _approval — the inline special-case
# ===========================================================================


class _RecordingToolManager:
    """Stand-in for ToolExecutionManager.execute(call, bypass_approval=True).
    Records the bypass-approval call and returns a synthetic ToolResultPart.
    """

    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, call, *, bypass_approval=False, principal=None):
        self.calls.append((call, bypass_approval))
        return ToolResultPart(
            id=call.id,
            output=f"approved-dispatch-of-{call.name}",
            error=False,
        )


async def test_resume_branch_special_cases_approval_with_bypass(
    scheduler, monkeypatch,
):
    """When parked_state.yielded.tool_name=='_approval' AND the
    resume payload decision=='approved', the worker re-dispatches the
    original tool call with bypass_approval=True so the gate doesn't
    re-trip on the second pass.
    """
    sid = "sess-resume-approval"
    tool_call_id = "tc-fs-delete-1"

    # The LLM emitted the original tool_use (fs.delete) — the
    # approval gate raised YieldToWorker mid-execute.
    assistant_msg = Message(
        role="assistant",
        parts=[
            ToolCallPart(
                id=tool_call_id,
                name="_workspaces__fs.delete",
                arguments={"path": "/etc/passwd"},
            ),
        ],
    )

    fake_session = _make_resumable_session(
        sid,
        tool_name="_approval",
        tool_call_id=tool_call_id,
        # Operator answered "approved" via /v1/.../tool_approval/respond.
        resume_event_payload={"decision": "approved"},
        resume_metadata={
            "policy_id": "pol-test",
            "approval_type": "required",
            "gate_reason": "destructive path",
            "original_call": {
                "id": tool_call_id,
                "name": "_workspaces__fs.delete",
                "arguments": {"path": "/etc/passwd"},
            },
        },
        llm_messages=[assistant_msg.model_dump(mode="json")],
    )
    scheduler.register_session_for_test(sid, status=SessionStatus.RUNNING)
    scheduler._sessions[sid] = fake_session   # type: ignore[attr-defined]

    pool = _build_pool(scheduler)
    pool._worker_id = "wrk-approve"
    await scheduler.register_worker(
        worker_id="wrk-approve", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-approve", runnable=True)
    lease = _make_sched_lease(sid, "wrk-approve")

    tool_manager = _RecordingToolManager()
    fake_executor = _RecordingExecutor(tool_manager=tool_manager)
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    await pool._run_one_turn(lease)

    # 1. bypass_approval=True dispatch happened against the original call.
    assert len(tool_manager.calls) == 1
    call, bypass = tool_manager.calls[0]
    assert bypass is True, "approved path must set bypass_approval=True"
    assert call.id == tool_call_id
    assert call.name == "_workspaces__fs.delete"

    # 2. Injected pair carries the dispatched tool result.
    assert len(fake_executor.injected) == 1
    injected = fake_executor.injected[0]
    tool_part = next(
        p for p in injected[1].parts if isinstance(p, ToolResultPart)
    )
    assert tool_part.id == tool_call_id
    assert "approved-dispatch-of-_workspaces__fs.delete" in tool_part.output
    assert tool_part.error is False

    # 3. clear_park + complete_turn(RUNNING, re_enqueue=True).
    assert fake_session.parked_status is None
    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.RUNNING
    assert snapshot.turn_no == lease.turn_no + 1


async def test_resume_branch_approval_rejected_synthesises_error(
    scheduler, monkeypatch,
):
    """When the operator answered ``rejected``, the resume path must
    NOT re-dispatch — it synthesises a ToolResultPart(error=True)
    carrying the rejection reason, and persists [assistant, tool_result].
    """
    sid = "sess-resume-rejected"
    tool_call_id = "tc-fs-delete-2"

    assistant_msg = Message(
        role="assistant",
        parts=[
            ToolCallPart(
                id=tool_call_id,
                name="_workspaces__fs.delete",
                arguments={"path": "/etc/passwd"},
            ),
        ],
    )

    fake_session = _make_resumable_session(
        sid,
        tool_name="_approval",
        tool_call_id=tool_call_id,
        resume_event_payload={
            "decision": "rejected",
            "reason": "security review denied",
        },
        resume_metadata={
            "policy_id": "pol-test",
            "approval_type": "required",
            "original_call": {
                "id": tool_call_id,
                "name": "_workspaces__fs.delete",
                "arguments": {"path": "/etc/passwd"},
            },
        },
        llm_messages=[assistant_msg.model_dump(mode="json")],
    )
    scheduler.register_session_for_test(sid, status=SessionStatus.RUNNING)
    scheduler._sessions[sid] = fake_session   # type: ignore[attr-defined]

    pool = _build_pool(scheduler)
    pool._worker_id = "wrk-reject"
    await scheduler.register_worker(
        worker_id="wrk-reject", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-reject", runnable=True)
    lease = _make_sched_lease(sid, "wrk-reject")

    tool_manager = _RecordingToolManager()
    fake_executor = _RecordingExecutor(tool_manager=tool_manager)
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    await pool._run_one_turn(lease)

    # Rejected path: NO bypass_approval dispatch should have happened.
    assert tool_manager.calls == [], (
        f"rejected path must NOT re-dispatch; got: {tool_manager.calls!r}"
    )

    # Synthetic ToolResultPart(error=True, output contains reason).
    injected = fake_executor.injected[0]
    tool_part = next(
        p for p in injected[1].parts if isinstance(p, ToolResultPart)
    )
    assert tool_part.error is True
    body = json.loads(tool_part.output)
    assert body["rejected"] is True
    assert "security review denied" in body["reason"]

    # Clear-park + complete_turn(RUNNING) still ran — the LLM gets to
    # see the rejection and decide what to do next.
    assert fake_session.parked_status is None
    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.RUNNING


# ===========================================================================
# cancel_requested wins over resumable (spec §7.3 step 3 / §7.4)
# ===========================================================================


async def test_resume_branch_does_not_fire_when_cancel_requested(
    scheduler, monkeypatch,
):
    """Cancel-during-park terminates the session WITHOUT calling the
    resume hook. Also clears the parked columns so an ENDED row
    doesn't leave dead park state behind.
    """
    sid = "sess-resume-then-cancel"
    tool_call_id = "tc-cancel"

    fake_session = _make_resumable_session(
        sid,
        tool_name="sleep",
        tool_call_id=tool_call_id,
        resume_event_payload={},
        resume_metadata={"requested_seconds": 30.0},
    )
    # The cancel API races the timer publish — the row arrives
    # at the worker with BOTH parked_status='resumable' AND
    # cancel_requested=True.
    fake_session.cancel_requested = True

    scheduler.register_session_for_test(sid, status=SessionStatus.RUNNING)
    scheduler._sessions[sid] = fake_session   # type: ignore[attr-defined]

    pool = _build_pool(scheduler)
    pool._worker_id = "wrk-cancel"
    await scheduler.register_worker(
        worker_id="wrk-cancel", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    scheduler._leases[sid] = _LeaseState(worker_id="wrk-cancel", runnable=True)
    lease = _make_sched_lease(sid, "wrk-cancel")

    fake_executor = _RecordingExecutor()
    monkeypatch.setattr(
        pool, "_load_session", lambda _sid: _async_return(fake_session),
    )
    # _build_agent_executor MUST NOT be called — the cancel branch
    # short-circuits before any executor is built.
    def _fail(*a, **kw):
        raise AssertionError(
            "_build_agent_executor must NOT run for cancel-during-park"
        )
    monkeypatch.setattr(pool, "_build_agent_executor", _fail)

    await pool._run_one_turn(lease)

    # WorkspaceSession ENDED; parked columns cleared. (InMemoryScheduler's
    # complete_turn doesn't persist ended_reason today — that's a
    # pre-existing gap; the postgres impl does. We assert on status
    # only here.)
    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.ENDED
    assert fake_session.parked_status is None
    assert fake_executor.injected == [], "executor injected nothing"
