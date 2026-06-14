"""A session resume on an ``_approval`` park writes a durable
ToolApprovalRecord exactly once, for every decision (approve/reject/timeout).

Drives the real WorkerPool engine resume branch (mirrors
test_engine_session_resume.py) and asserts the persisted record.
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
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState

from tests.conftest import _FakeStorageProvider


async def _async_return(value):
    return value


class _FakeToolManager:
    async def execute(self, call, *, bypass_approval=False):
        return ToolResultPart(id=call.id, output=json.dumps({"ran": True}), error=False)


class _RecordingExecutor:
    def __init__(self, *, tool_manager=None):
        self._tool_manager = tool_manager
        self.injected: list = []

    async def inject_resume_messages(self, messages):
        self.injected.append(list(messages))


class _NoopPersist:
    pass


def _approval_session(sid: str, *, tcid: str, resume_payload):
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    assistant_msg = Message(
        role="assistant",
        parts=[ToolCallPart(id=tcid, name="delete_workspace", arguments={"id": "ws-1"})],
    )
    yielded = Yielded(
        tool_name="_approval",
        event_key=f"tool_approval:{sid}:{tcid}",
        timeout=600.0,
        resume_metadata={
            "parked_at_iso": parked_at.isoformat(),
            "policy_id": "p1",
            "approval_type": "required",
            "gate_reason": "always-on",
            "original_call": {
                "id": tcid,
                "name": "delete_workspace",
                "arguments": {"id": "ws-1"},
            },
        },
    )
    parked_state = ParkedState(
        yielded=yielded,
        llm_messages=[assistant_msg.model_dump(mode="json")],
        turn_no=0,
        started_at=parked_at,
        tool_call_id=tcid,
        resume_event_payload=resume_payload,
    )
    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        parked_status="resumable",
        parked_event_key=f"tool_approval:{sid}:{tcid}",
        parked_until=parked_at + timedelta(seconds=600),
        parked_at=parked_at,
        parked_state=parked_state.to_jsonable(),
    )


async def _drive(monkeypatch, sess):
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage)},
    )
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                  # type: ignore[arg-type]
        storage=storage_provider,
        workspace_registry=None,         # type: ignore[arg-type]
        provider_registry=None,          # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-approval-record"
    await session_storage.create(sess)
    fake_executor = _RecordingExecutor(tool_manager=_FakeToolManager())
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )
    await engine.mark_resumable(ClaimKind.SESSION, sess.id)
    leases = await engine.claim_due("wrk-approval-record", max_count=10)
    lease = next(
        ln for ln in leases
        if ln.kind == ClaimKind.SESSION and ln.entity_id == sess.id
    )
    await pool._run_engine_session(lease)
    records = await storage_provider.get_storage(ToolApprovalRecord).list(
        OffsetPage(offset=0, length=50)
    )
    return records.items


@pytest.mark.asyncio
async def test_resume_approved_writes_record(monkeypatch):
    sess = _approval_session(
        "sess-rec-approve", tcid="tc1", resume_payload={"decision": "approved"},
    )
    items = await _drive(monkeypatch, sess)
    assert len(items) == 1
    rec = items[0]
    assert rec.decision == "approved"
    assert rec.tool_name == "delete_workspace"
    assert rec.arguments == {"id": "ws-1"}
    assert rec.tool_call_id == "tc1"
    assert rec.session_id == "sess-rec-approve"
    assert rec.agent_id == "ag-1"
    assert rec.policy_id == "p1"
    assert rec.approval_type == "required"
    assert rec.gate_reason == "always-on"
    assert rec.requested_at is not None


@pytest.mark.asyncio
async def test_resume_rejected_writes_record(monkeypatch):
    sess = _approval_session(
        "sess-rec-reject", tcid="tc2",
        resume_payload={"decision": "rejected", "reason": "too risky"},
    )
    items = await _drive(monkeypatch, sess)
    assert len(items) == 1
    assert items[0].decision == "rejected"
    assert items[0].reason == "too risky"


@pytest.mark.asyncio
async def test_resume_timeout_writes_record(monkeypatch):
    # The timeout marker key in resume_event_payload -> the resume classifies
    # as a YieldTimeout, which becomes a rejected/timed-out decision.
    sess = _approval_session(
        "sess-rec-timeout", tcid="tc3", resume_payload={"__yield_timeout__": True},
    )
    items = await _drive(monkeypatch, sess)
    assert len(items) == 1
    # classify_approval_payload maps a YieldTimeout to ("rejected","timed-out").
    assert items[0].decision == "rejected"
    assert items[0].reason == "timed-out"


@pytest.mark.asyncio
async def test_resume_writes_record_exactly_once(monkeypatch):
    sess = _approval_session(
        "sess-rec-once", tcid="tc4", resume_payload={"decision": "approved"},
    )
    items = await _drive(monkeypatch, sess)
    assert len(items) == 1
