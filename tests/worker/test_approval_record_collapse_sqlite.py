"""The respond-time write and the worker's resume-time fallback of ONE gated decision collapse into ONE row on a real unique index (C-033 PR 2, #673 review round 2).

Driven through the real respond helper (``_publish_decision``, what ``POST .../tool_approval/respond`` calls after its guards) and the real ``WorkerPool`` engine
resume branch, against ``SqliteStorageProvider``: a respond, a retried respond and the resume of one gate leave one row (``<event key>@<gate id>``); a second round
that repeats the raw id under a new gate writes a SECOND row (it used to collide with the first and be dropped); a park from before gates had ids keeps the bare key
and collapses on it; and during a rolling deploy a decision whose respond-time write ran on an old pod (the bare key) and whose resume ran on a new one (the
suffixed key) leaves two rows that agree. (The API test double enforces no unique index, so this is where the collapse is proven.)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.agent.approval_record import record_from_parked_blob, write_approval_record
from primer.api.routers.tool_approval import ToolApprovalRespondBody, _publish_decision
from primer.bus.in_memory import InMemoryEventBus
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.scheduler import WorkerConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import WorkspaceSession
from primer.session.pending_gates import resolve_pending_gate
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.pool import WorkerPool
from tests.worker.test_approval_record_resume import (
    _approval_session,
    _async_return,
    _FakeToolManager,
    _NoopPersist,
    _RecordingExecutor,
)

SID, RAW = "sess-collapse", "call_0"
G1, G2 = "1" * 32, "2" * 32
KEY = f"tool_approval:{SID}:{RAW}"


def _parked(gate_id: str | None) -> WorkspaceSession:
    sess = _approval_session(SID, tcid=RAW, resume_payload=None)
    if gate_id is not None:
        sess.parked_state["yielded"]["resume_metadata"]["gate_id"] = gate_id
    return sess.model_copy(update={"parked_status": "parked"})


async def _respond(sp, bus, *, gate_id: str | None, decision: str, by: str) -> None:
    ss = sp.get_storage(WorkspaceSession)
    row = await ss.get(SID)
    gate = resolve_pending_gate(row.parked_state, tool_call_id=RAW, kind="_approval", gate_id=gate_id)
    assert gate is not None
    body = ToolApprovalRespondBody(tool_call_id=RAW, gate_id=gate_id, decision=decision)
    await _publish_decision(
        sess=row, id_str=SID, body=body, gate=gate, event_bus=bus, session_storage=ss, engine=None, storage_provider=sp, decided_by=by,
    )


async def _resume(monkeypatch, sp, engine) -> _RecordingExecutor:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=sp,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-collapse"
    executor = _RecordingExecutor(tool_manager=_FakeToolManager())
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _w: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(executor))
    await engine.mark_resumable(ClaimKind.SESSION, SID)
    lease = next(ln for ln in await engine.claim_due("wrk-collapse", max_count=10) if ln.entity_id == SID)
    await pool._run_engine_session(lease)
    return executor


async def _rows(sp) -> list[tuple]:
    items = (await sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=50))).items
    return sorted((r.gate_event_key, r.decision, r.decided_by) for r in items)


@pytest.fixture
async def world(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "collapse.sqlite")))
    await sp.initialize()
    bus = InMemoryEventBus()
    await bus.initialize()
    engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=sp.get_storage(WorkspaceSession))})
    try:
        yield sp, bus, engine
    finally:
        await bus.aclose()
        await sp.aclose()


@pytest.mark.asyncio
async def test_respond_retry_and_resume_of_one_gate_leave_one_row(monkeypatch, world):
    sp, bus, engine = world
    await sp.get_storage(WorkspaceSession).create(_parked(G1))

    await _respond(sp, bus, gate_id=G1, decision="approved", by="alice")
    await _respond(sp, bus, gate_id=G1, decision="approved", by="alice")   # a retried respond (row already resumable)
    executor = await _resume(monkeypatch, sp, engine)

    assert executor.injected, "the session did not resume"
    assert await _rows(sp) == [(f"{KEY}@{G1}", "approved", "alice")]


@pytest.mark.asyncio
async def test_two_rounds_under_one_raw_id_write_two_rows(monkeypatch, world):
    sp, bus, engine = world
    ss = sp.get_storage(WorkspaceSession)
    await ss.create(_parked(G1))
    await _respond(sp, bus, gate_id=G1, decision="approved", by="alice")
    await _resume(monkeypatch, sp, engine)

    # round 3: the same raw id parks again under a new gate (expire round 1's kept lease, as the author's worker test does)
    from datetime import timedelta

    engine._leases[(ClaimKind.SESSION, SID)].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await ss.update(_parked(G2))
    await _respond(sp, bus, gate_id=None, decision="rejected", by="bob")     # a tokenless respond still lands on the PARK's gate key
    await _resume(monkeypatch, sp, engine)

    assert await _rows(sp) == [(f"{KEY}@{G1}", "approved", "alice"), (f"{KEY}@{G2}", "rejected", "bob")]


@pytest.mark.asyncio
async def test_legacy_park_keeps_the_bare_key_and_collapses(monkeypatch, world):
    sp, bus, engine = world
    await sp.get_storage(WorkspaceSession).create(_parked(None))
    await _respond(sp, bus, gate_id=None, decision="approved", by="alice")
    await _resume(monkeypatch, sp, engine)

    assert await _rows(sp) == [(KEY, "approved", "alice")]


@pytest.mark.asyncio
async def test_rolling_deploy_old_pod_respond_new_pod_resume_leaves_two_agreeing_rows(monkeypatch, world, caplog):
    """The deploy note's window: the respond-time write ran on a pod WITHOUT this change (bare key), the resume on one WITH it."""
    import logging

    sp, bus, engine = world
    ss = sp.get_storage(WorkspaceSession)
    await ss.create(_parked(G1))
    row = await ss.get(SID)
    # what the previous version's respond route wrote: the same builder, minus the gate id in the key
    blob = {"tool_call_id": RAW, "yielded": {"resume_metadata": {k: v for k, v in row.parked_state["yielded"]["resume_metadata"].items() if k != "gate_id"}}}
    old = record_from_parked_blob(blob=blob, decision="approved", reason=None, session_id=SID, decided_by="alice", gate_event_key=KEY)
    await write_approval_record(sp.get_storage(ToolApprovalRecord), old)
    from primer.session.yields import durably_wake_session

    await durably_wake_session(row, event_key=KEY, payload={"decision": "approved", "reason": None, "decided_by": "alice"}, session_storage=ss, engine=None)
    with caplog.at_level(logging.ERROR, logger="primer.agent.approval_record"):
        await _resume(monkeypatch, sp, engine)

    assert await _rows(sp) == [(KEY, "approved", "alice"), (f"{KEY}@{G1}", "approved", "alice")]
    assert not [r for r in caplog.records if "disagreement" in r.getMessage()]
