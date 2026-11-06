"""GR-3 (Task 103), the review's probe inverted: a reply to a multi-event (graph) park that lands WHILE the first reply's resume runs survives
the re-park.

Graph park on two gates A and B. A's approval flips the row resumable; a worker claims it and starts the resume. While it runs, the human
approves B: ``durably_mark_session_resumable`` accepts it (the respond route answers 202) and accumulates B into ``resume_event_payloads``.
The resume then re-parks on B with the real re-park builder (``repark_graph_outcome``). B's answer must still be on the row, the row
``resumable`` and its lease armed, so the next claim runs it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.yields import durably_mark_session_resumable
from primer.worker.graph_resume_coordinator import repark_graph_outcome
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState
from primer.workspace.session_factory import pause_session
from tests.conftest import _FakeStorageProvider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [SessionStatus.CREATED, SessionStatus.RUNNING, SessionStatus.WAITING],
)
async def test_second_reply_landing_mid_resume_survives_the_repark(monkeypatch, initial_status):
    sid = "sess-g1-multi"
    key_a = f"tool_approval:{sid}:node-a:tc-a"
    key_b = f"tool_approval:{sid}:node-b:tc-b"
    storage = _FakeStorageProvider()
    sessions = storage.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=sessions)})
    pool = WorkerPool(config=WorkerConfig(concurrency=1), scheduler=None, storage=storage, workspace_registry=None,
                      provider_registry=None, engine=engine, event_bus=None)
    pool._worker_id = "wrk-g1"

    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    blob = ParkedState(
        yielded=Yielded(tool_name="_approval", event_key=key_a, resume_metadata={}, event_keys=[key_a, key_b]),
        llm_messages=[], turn_no=0, started_at=parked_at, tool_call_id="tc-a", graph_checkpoint={"nodes": {}},
    ).to_jsonable()
    await sessions.create(WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=GraphSessionBinding(kind="graph", graph_id="g-1"),
        status=initial_status, created_at=datetime.now(timezone.utc),
        parked_status="parked", parked_event_key=key_a, parked_event_keys=[key_a, key_b],
        parked_until=parked_at + timedelta(seconds=3600), parked_at=parked_at, parked_state=blob,
    ))

    # Reply A lands and arms the lease.
    assert await durably_mark_session_resumable(await sessions.get(sid), event_key=key_a, payload={"decision": "approved"},
                                                session_storage=sessions, engine=engine)
    lease = next(l for l in await engine.claim_due("wrk-g1", max_count=10) if l.entity_id == sid)

    b_accepted = {}
    handler_park_keys = []

    async def _resume_graph_engine(session, parked):
        handler_park_keys.append(session.parked_event_key)
        if len(handler_park_keys) == 2:
            return ReleaseOutcome(success=True, drop_lease=True)
        # While A's resume runs, the human approves B: accepted on the resumable multi-event row.
        b_accepted["did"] = await durably_mark_session_resumable(
            await sessions.get(sid), event_key=key_b, payload={"decision": "approved"}, session_storage=sessions, engine=engine,
        )
        mid = await sessions.get(sid)
        b_accepted["stored"] = any(e.get("event_key") == key_b for e in (mid.parked_state.get("resume_event_payloads") or {}).values())
        # A drained; B is still pending in the checkpoint this resume read: the real coordinator re-parks on it.
        repark = YieldToWorker(Yielded(tool_name="_approval", event_key=key_b, resume_metadata={}, event_keys=[key_b]), tool_call_id="tc-b")
        repark.graph_checkpoint = {"nodes": {}}
        return repark_graph_outcome(pool, session, repark)

    monkeypatch.setattr(pool, "_resume_graph_engine", _resume_graph_engine)
    await pool._run_engine_session(lease)

    assert b_accepted == {"did": True, "stored": True}, "B's approval was accepted and stored mid-resume"
    row = await sessions.get(sid)
    # Fixed: the re-park waits on B, holds B's accepted answer, and has a lease to run it.
    assert row.parked_status == "resumable" and row.parked_event_key == key_b
    payloads = (row.parked_state or {}).get("resume_event_payloads") or {}
    assert [e.get("event_key") for e in payloads.values()] == [key_b]
    assert await engine.has_lease(ClaimKind.SESSION, sid)
    next_leases = await engine.claim_due("wrk-g2", max_count=10)
    assert [lease.entity_id for lease in next_leases] == [sid]
    await pool._run_engine_session(next_leases[0])

    assert handler_park_keys == [key_a, key_b]
    assert await engine.claim_due("wrk-g3", max_count=10) == []
    assert not await engine.has_lease(ClaimKind.SESSION, sid)


@pytest.mark.asyncio
async def test_pause_between_carried_reply_read_and_rearm_never_runs_the_resume_handler(monkeypatch):
    sid = "sess-g1-pause-race"
    key = f"tool_approval:{sid}:node-b:tc-b"
    storage = _FakeStorageProvider()
    sessions = storage.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=sessions)})
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,
        storage=storage,
        workspace_registry=None,
        provider_registry=None,
        engine=engine,
        event_bus=None,
    )
    pool._worker_id = "wrk-pause-race"
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    await sessions.create(WorkspaceSession(
        id=sid,
        workspace_id="ws-1",
        binding=GraphSessionBinding(kind="graph", graph_id="g-1"),
        status=SessionStatus.CREATED,
        created_at=datetime.now(timezone.utc),
        parked_status="resumable",
        parked_event_key=key,
        parked_event_keys=[key],
        parked_at=parked_at,
        parked_state={"resume_event_payloads": {"b": {"event_key": key, "payload": {"decision": "approved"}}}},
    ))

    real_mark_resumable = engine.mark_resumable

    async def pause_then_mark(kind, entity_id, **kwargs):
        paused = await pause_session(
            workspace_id="ws-1",
            session_id=sid,
            sessions=sessions,
        )
        assert paused.status is SessionStatus.PAUSED
        await real_mark_resumable(kind, entity_id, **kwargs)

    engine.mark_resumable = pause_then_mark  # type: ignore[method-assign]
    await pool._rearm_carried_reply(sid)
    leases = await engine.claim_due("wrk-pause-race", max_count=10)
    assert [lease.entity_id for lease in leases] == [sid]

    resume_runs = 0

    async def resume_must_not_run(session, parked):
        nonlocal resume_runs
        resume_runs += 1
        return ReleaseOutcome(success=True, drop_lease=True)

    monkeypatch.setattr(pool, "_resume_graph_engine", resume_must_not_run)
    await pool._run_engine_session(leases[0])

    row = await sessions.get(sid)
    assert row is not None
    assert row.status is SessionStatus.PAUSED and row.parked_status == "resumable"
    assert resume_runs == 0
    assert not await engine.has_lease(ClaimKind.SESSION, sid)
