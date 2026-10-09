"""The resume-time audit record is keyed by the GATE that was decided, through the worker paths (C-033 PR 2, ticket 01a11f52-9d98).

The unit tests of the key (``tests/agent/test_approval_record_key.py``) and the respond route (``tests/api/test_approval_record_key.py``) pass the gate id in
by hand; the worker tests that already existed park WITHOUT one, so they only ever saw the bare event key. These park WITH a gate id, as every park does
since C-033 PR 1, and drive the two resume-time writers:

* the session resume (the real ``WorkerPool`` engine branch): the record carries ``<event key>@<gate id>``, and a second round of the same session that
  repeats the provider's ``tool_call_id`` under a new gate writes its OWN row instead of colliding with the first;
* the graph resume fallback (``write_approval_record_for_graph``): two fan-out siblings that share one raw id each get the record of the gate that was
  decided, selected by the event key the decision fired (C-033 PR 3), not the first entry with that raw id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.scheduler import WorkerConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import WorkspaceSession
from primer.worker.graph_resume_coordinator import write_approval_record_for_graph
from primer.worker.pool import WorkerPool
from tests.conftest import _FakeStorageProvider
from tests.worker.test_approval_record_resume import (
    _approval_session,
    _async_return,
    _FakeToolManager,
    _NoopPersist,
    _RecordingExecutor,
)

G1, G2 = "a" * 32, "b" * 32
RAW = "call_0"
SID = "sess-gate-key"


def _with_gate(sess: WorkspaceSession, gate_id: str) -> WorkspaceSession:
    sess.parked_state["yielded"]["resume_metadata"]["gate_id"] = gate_id
    return sess


async def _records(storage_provider) -> list[ToolApprovalRecord]:
    return (await storage_provider.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=50))).items


async def _rounds(monkeypatch, *rounds: WorkspaceSession) -> list[ToolApprovalRecord]:
    """Resume ``rounds`` one after the other on ONE session row (same id, same raw tool_call_id) through the real pool, then read the records."""
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage)})
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=storage_provider,  # type: ignore[arg-type]
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-gate-key"
    executor = _RecordingExecutor(tool_manager=_FakeToolManager())
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _ws_id: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(executor))
    for number, sess in enumerate(rounds):
        if number == 0:
            await session_storage.create(sess)
        else:
            engine._leases[(ClaimKind.SESSION, sess.id)].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session_storage.update(sess)
        await engine.mark_resumable(ClaimKind.SESSION, sess.id)
        lease = next(ln for ln in await engine.claim_due("wrk-gate-key", max_count=10) if ln.entity_id == sess.id)
        await pool._run_engine_session(lease)
    return await _records(storage_provider)


@pytest.mark.asyncio
async def test_a_session_resume_records_the_gate_it_resumed(monkeypatch):
    sess = _with_gate(_approval_session(SID, tcid=RAW, resume_payload={"decision": "approved"}), G1)

    records = await _rounds(monkeypatch, sess)

    assert [r.gate_event_key for r in records] == [f"tool_approval:{SID}:{RAW}@{G1}"]


@pytest.mark.asyncio
async def test_a_second_round_that_repeats_the_raw_id_under_a_new_gate_writes_its_own_record(monkeypatch):
    first = _with_gate(_approval_session(SID, tcid=RAW, resume_payload={"decision": "approved"}), G1)
    third = _with_gate(_approval_session(SID, tcid=RAW, resume_payload={"decision": "rejected", "reason": "no"}), G2)

    records = await _rounds(monkeypatch, first, third)

    assert sorted(r.gate_event_key for r in records) == sorted([f"tool_approval:{SID}:{RAW}@{G1}", f"tool_approval:{SID}:{RAW}@{G2}"]), (
        "the second approval of a repeated raw id collided with the first and was dropped"
    )
    by_gate = {r.gate_event_key.rsplit("@", 1)[-1]: r for r in records}
    assert by_gate[G1].decision == "approved" and by_gate[G2].decision == "rejected", "each record carries its own decision"


@pytest.mark.asyncio
async def test_a_park_from_before_gates_had_ids_keeps_the_bare_event_key(monkeypatch):
    sess = _approval_session(SID, tcid=RAW, resume_payload={"decision": "approved"})

    records = await _rounds(monkeypatch, sess)

    assert [r.gate_event_key for r in records] == [f"tool_approval:{SID}:{RAW}"]


# ---- the graph resume fallback ---------------------------------------------------------------------------------------------------------------------


def _sibling(node: str, gate_id: str) -> dict:
    return {
        "node_id": node, "tool_call_id": RAW, "event_key": f"tool_approval:{SID}:{node}:{RAW}", "tool_name": "_approval",
        "resume_metadata": {
            "policy_id": "pol", "approval_type": "required", "gate_reason": "matched policy", "approvers": None, "gate_id": gate_id,
            "original_call": {"id": RAW, "name": "delete_workspace", "arguments": {"id": f"ws-{node}"}},
        },
        "llm_messages": [], "iteration": 0, "frames": [], "leaf": None,
    }


def _graph_session() -> SimpleNamespace:
    return SimpleNamespace(id=SID, binding=SimpleNamespace(agent_id="agt"), parked_at=None)


async def _decide(storage_provider, node: str, decision: str) -> None:
    checkpoint = {"pending_toolcalls": [], "pending_agent_yields": [_sibling("A", G1), _sibling("B", G2)], "pending_dispatch": []}
    fired = f"tool_approval:{SID}:{node}:{RAW}"
    await write_approval_record_for_graph(
        SimpleNamespace(_storage=storage_provider), session=_graph_session(), checkpoint=checkpoint, tcid=fired.rsplit(":", 1)[-1],
        payload={"decision": decision, "reason": None, "decided_by": "x"}, event_key=fired,
    )


@pytest.mark.asyncio
async def test_the_graph_fallback_records_the_sibling_that_was_decided():
    sp = _FakeStorageProvider()

    await _decide(sp, "B", "approved")

    assert [r.gate_event_key for r in await _records(sp)] == [f"tool_approval:{SID}:B:{RAW}@{G2}"], "B's decision was recorded under another sibling's gate"


@pytest.mark.asyncio
async def test_both_siblings_of_one_raw_id_keep_their_own_record():
    sp = _FakeStorageProvider()

    await _decide(sp, "B", "approved")
    await _decide(sp, "A", "rejected")

    keys = sorted(r.gate_event_key for r in await _records(sp))
    assert keys == sorted([f"tool_approval:{SID}:A:{RAW}@{G1}", f"tool_approval:{SID}:B:{RAW}@{G2}"])
