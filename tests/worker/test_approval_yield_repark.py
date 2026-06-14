"""Two-phase park: an approval gate on a *yielding* agent-session tool.

When an approval gate sits on a yielding tool the park is two-phase:

* Phase 1 - the session parks for the operator's approval decision
  (``tool_name='_approval'``).
* Phase 2 - once APPROVED, ``_resume_tool_approval`` re-dispatches the
  real tool with ``bypass_approval=True``; if that tool is a yielding
  tool it raises :class:`YieldToWorker` a second time. The resume path
  must RE-PARK on the tool's real event key (not synthesise a fail-closed
  error tool_result), and resume to completion when the real event fires.

A REJECT still short-circuits to a clean error tool_result (the tool
never runs) - the existing fail-closed behaviour.

Driven end-to-end through ``WorkerPool._run_engine_session`` with a real
InMemoryClaimEngine + SessionClaimAdapter so the on_release park-column
book-keeping is exercised, mirroring tests/worker/test_engine_session_resume.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState

from tests.conftest import _FakeStorageProvider


async def _async_return(value):
    return value


class _RecordingExecutor:
    def __init__(self, *, tool_manager=None):
        self._tool_manager = tool_manager
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages):
        self.injected.append(list(messages))


class _YieldingToolManager:
    """Fake ToolExecutionManager whose execute() yields once (the real
    tool's own event) when re-dispatched with bypass_approval=True."""

    def __init__(self, *, yielded: Yielded, tool_call_id: str):
        self._yielded = yielded
        self._tool_call_id = tool_call_id
        self.calls: list[ToolCallPart] = []

    async def execute(self, call: ToolCallPart, *, bypass_approval: bool = False):
        self.calls.append(call)
        # The approved tool runs and yields for its OWN real event.
        raise YieldToWorker(self._yielded, tool_call_id=self._tool_call_id)


class _NoopPersist:
    pass


def _build_engine(session_storage) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )


def _build_pool(storage, engine) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                  # type: ignore[arg-type]
        storage=storage,
        workspace_registry=None,         # type: ignore[arg-type]
        provider_registry=None,          # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-approval-yield"
    return pool


def _make_approval_parked_session(
    sid: str,
    *,
    tool_call_id: str,
    real_tool_name: str,
    real_arguments: dict,
    resume_event_payload: dict,
    llm_messages: list,
) -> WorkspaceSession:
    """A session parked (phase 1) on an approval gate guarding a yielding
    tool. resume_metadata carries the original_call so the approved resume
    re-dispatches the real tool."""
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    yielded = Yielded(
        tool_name="_approval",
        event_key=f"tool_approval:{sid}:{tool_call_id}",
        timeout=600.0,
        resume_metadata={
            "parked_at_iso": parked_at.isoformat(),
            "original_call": {
                "id": tool_call_id,
                "name": real_tool_name,
                "arguments": real_arguments,
            },
        },
    )
    parked_state = ParkedState(
        yielded=yielded,
        llm_messages=llm_messages,
        turn_no=0,
        started_at=parked_at,
        tool_call_id=tool_call_id,
        resume_event_payload=resume_event_payload,
    )
    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        parked_status="resumable",
        parked_event_key=f"tool_approval:{sid}:{tool_call_id}",
        parked_until=parked_at + timedelta(seconds=600),
        parked_at=parked_at,
        parked_state=parked_state.to_jsonable(),
    )


async def _claim_session(engine, sid: str):
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    leases = await engine.claim_due("wrk-approval-yield", max_count=10)
    for lease in leases:
        if lease.kind == ClaimKind.SESSION and lease.entity_id == sid:
            return lease
    raise AssertionError(f"no claimable lease for session {sid!r}")


def _assistant_msg(tool_call_id: str, real_tool_name: str) -> Message:
    return Message(
        role="assistant",
        parts=[
            ToolCallPart(
                id=tool_call_id,
                name=real_tool_name,
                arguments={"seconds": 30},
            ),
        ],
    )


@pytest.mark.asyncio
async def test_approved_yielding_tool_reparks_then_resumes(monkeypatch):
    """APPROVE an approval-gated yielding tool -> the session RE-PARKS on the
    tool's real event key (NOT an error tool_result, NOT ended). Then the real
    event fires -> the session resumes to completion via the tool resume hook."""
    sid = "sess-approval-yield-ok"
    tool_call_id = "tc-sleep"
    real_tool_name = "_test_yield_repark_tool"

    # Register a resume hook for the real tool so phase-2 resume completes.
    from primer.worker.yield_resume_registry import register_resume_hook

    class _HookResult:
        def __init__(self, output, is_error=False):
            self.output = output
            self.is_error = is_error

    def _hook(resume_metadata, payload):
        return _HookResult(json.dumps({"woke": True, "payload": payload}))

    register_resume_hook(real_tool_name, _hook)

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    # The real tool's own yield (phase 2): a timer event, NOT an approval.
    real_event = Yielded(
        tool_name=real_tool_name,
        event_key=f"timer:{tool_call_id}",
        timeout=120.0,
        resume_metadata={"seconds": 30},
    )
    tm = _YieldingToolManager(yielded=real_event, tool_call_id=tool_call_id)
    fake_executor = _RecordingExecutor(tool_manager=tm)

    sess = _make_approval_parked_session(
        sid,
        tool_call_id=tool_call_id,
        real_tool_name=real_tool_name,
        real_arguments={"seconds": 30},
        resume_event_payload={"decision": "approved"},
        llm_messages=[_assistant_msg(tool_call_id, real_tool_name).model_dump(mode="json")],
    )
    await session_storage.create(sess)

    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    lease = await _claim_session(engine, sid)
    await pool._run_engine_session(lease)

    # The approved tool was re-dispatched with bypass_approval (it yielded).
    assert len(tm.calls) == 1
    # No error tool_result was injected - the session re-parked instead.
    assert fake_executor.injected == []

    row = await session_storage.get(sid)
    assert row is not None
    # Phase 2: re-parked on the tool's REAL event key, not ended/failed.
    assert row.status != SessionStatus.ENDED
    assert row.parked_status == "parked"
    assert row.parked_event_key == f"timer:{tool_call_id}"
    # The re-park stamped the real tool name into the blob (so the eventual
    # real-event resume routes to the tool's resume hook, not re-dispatch).
    assert row.parked_state["yielded"]["tool_name"] == real_tool_name
    # The in-progress turn messages were preserved across the re-park.
    assert row.parked_state["llm_messages"]

    # ---- Phase 2 resume: the real (timer) event fires -> resume to done. ----
    # Re-stamp the row as resumable carrying the real event payload, mirroring
    # what the listener leaves behind when the timer wakes the park.
    new_blob = dict(row.parked_state)
    new_blob["resume_event_payload"] = {"woke_at": "now"}
    row2 = row.model_copy(update={
        "parked_status": "resumable",
        "parked_state": new_blob,
    })
    await session_storage.update(row2)

    # A fresh executor for the continuation (real tool has no tool_manager
    # call this time - it goes through the resume hook).
    fake_executor2 = _RecordingExecutor(tool_manager=tm)
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor2),
    )
    lease2 = await _claim_session(engine, sid)
    await pool._run_engine_session(lease2)

    # The resume hook produced a tool_result that got injected (turn continues).
    assert len(fake_executor2.injected) == 1
    injected = fake_executor2.injected[0]
    assert injected[-1].role == "tool"
    tool_part = next(p for p in injected[-1].parts if isinstance(p, ToolResultPart))
    assert tool_part.id == tool_call_id
    assert tool_part.error is False
    body = json.loads(tool_part.output)
    assert body["woke"] is True

    row3 = await session_storage.get(sid)
    assert row3 is not None
    assert row3.parked_status is None  # park cleared, continuation runs
    assert row3.status != SessionStatus.ENDED


@pytest.mark.asyncio
async def test_rejected_yielding_tool_short_circuits_clean_error(monkeypatch):
    """REJECT the approval on a yielding tool -> a clean error tool_result is
    injected; the real tool never runs, the session is NOT re-parked."""
    sid = "sess-approval-yield-reject"
    tool_call_id = "tc-sleep-r"
    real_tool_name = "_test_yield_repark_tool_r"

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    # If execute() were ever called, it would yield - assert it is NOT.
    real_event = Yielded(tool_name=real_tool_name, event_key=f"timer:{tool_call_id}")
    tm = _YieldingToolManager(yielded=real_event, tool_call_id=tool_call_id)
    fake_executor = _RecordingExecutor(tool_manager=tm)

    sess = _make_approval_parked_session(
        sid,
        tool_call_id=tool_call_id,
        real_tool_name=real_tool_name,
        real_arguments={"seconds": 30},
        resume_event_payload={"decision": "rejected", "reason": "no thanks"},
        llm_messages=[_assistant_msg(tool_call_id, real_tool_name).model_dump(mode="json")],
    )
    await session_storage.create(sess)

    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    lease = await _claim_session(engine, sid)
    await pool._run_engine_session(lease)

    # The real tool was NEVER dispatched.
    assert tm.calls == []
    # A clean error tool_result was injected (short-circuit, not re-park).
    assert len(fake_executor.injected) == 1
    injected = fake_executor.injected[0]
    tool_part = next(p for p in injected[-1].parts if isinstance(p, ToolResultPart))
    assert tool_part.error is True
    body = json.loads(tool_part.output)
    assert body["rejected"] is True
    assert "no thanks" in body["reason"]

    row = await session_storage.get(sid)
    assert row is not None
    # NOT re-parked - the continuation turn runs (park cleared).
    assert row.parked_status is None
    assert row.status != SessionStatus.ENDED
