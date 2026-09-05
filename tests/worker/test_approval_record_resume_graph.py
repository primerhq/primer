"""Graph-path analogue of test_approval_record_resume.py.

A graph resume's per-gate approval decision must write a durable
ToolApprovalRecord scoped to the SPECIFIC entry actually decided, not
just the checkpoint's primary/first pending gate (01a06b82) -- the
resume-time counterpart of the REST second-of-N routing fix
(test_tool_approval_graph_routing.py). Calls
write_approval_record_for_graph directly rather than driving the full
pool resume loop: the function's own contract (resolve one entry by
tcid, write best-effort) is what's under test here, not the surrounding
drain machinery already covered elsewhere (test_pool_graph_resume.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding
from primer.worker.graph_resume_coordinator import write_approval_record_for_graph

from tests.conftest import _FakeStorageProvider


def _two_gate_checkpoint(session_id: str) -> dict:
    """Two concurrent fan-out ToolCall-node approval gates: worker[0]
    (call-0, the checkpoint's primary) and worker[1] (call-1, not
    projected onto any top-level blob -- only reachable through
    pending_toolcalls). Mirrors the graph-checkpoint shape
    _CheckpointMixin.snapshot_state actually persists.
    """
    def _entry(node_id: str, tool_call_id: str) -> dict:
        return {
            "node_id": node_id,
            "tool_call_id": tool_call_id,
            # UNSCOPED: see tests/api/test_tool_approval_graph_routing.py's
            # _two_gate_graph_parked_session._entry for why a ToolCall-node
            # gate's key never folds in node_id.
            "parked_event_key": f"tool_approval:{session_id}:{tool_call_id}",
            "arguments": {"id": f"ws-{node_id}"},
            "tool_name": "_approval",
            "resume_metadata": {
                "policy_id": "pol-fanout",
                "approval_type": "required",
                "gate_reason": "matched policy",
                "approvers": None,
                "original_call": {
                    "id": tool_call_id, "name": "delete_workspace",
                    "arguments": {"id": f"ws-{node_id}"},
                },
            },
            "scoped_tool_call_id": None,
        }

    entries = [_entry("worker[0]", "call-0"), _entry("worker[1]", "call-1")]
    return {
        "pending_toolcalls": entries,
        "pending_agent_yields": [],
        # The denormalised channel-prompt view: only original_call, no
        # policy_id/approvers -- proves the record read does NOT fall
        # back to this for its metadata.
        "pending_dispatch": [
            {
                "kind": "_approval",
                "node_id": e["node_id"],
                "tool_call_id": e["tool_call_id"],
                "resume_metadata": {
                    "original_call": e["resume_metadata"]["original_call"],
                },
            }
            for e in entries
        ],
    }


def _session(session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        binding=AgentSessionBinding(kind="agent", agent_id="agt-graph"),
        parked_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )


async def _records_for(storage_provider) -> list:
    page = await storage_provider.get_storage(ToolApprovalRecord).list(
        OffsetPage(offset=0, length=50),
    )
    return page.items


@pytest.mark.asyncio
async def test_resume_record_for_second_of_two_concurrent_gates_is_scoped_correctly():
    """The record for call-1 must carry call-1's own metadata + event_key,
    not call-0's (the checkpoint's primary/first pending entry)."""
    storage_provider = _FakeStorageProvider()
    pool = SimpleNamespace(_storage=storage_provider)
    session_id = "graph-rec-1"
    checkpoint = _two_gate_checkpoint(session_id)

    await write_approval_record_for_graph(
        pool, session=_session(session_id), checkpoint=checkpoint,
        tcid="call-1", payload={"decision": "approved", "decided_by": "alice"},
    )

    items = await _records_for(storage_provider)
    assert len(items) == 1
    rec = items[0]
    assert rec.tool_call_id == "call-1"
    assert rec.decision == "approved"
    assert rec.decided_by == "alice"
    assert rec.policy_id == "pol-fanout"
    assert rec.approval_type == "required"
    assert rec.gate_reason == "matched policy"
    assert rec.gate_event_key == f"tool_approval:{session_id}:call-1"


@pytest.mark.asyncio
async def test_resume_record_carries_full_policy_metadata_not_just_original_call():
    """Bugfix: resolving via pending_toolcalls (not pending_dispatch's
    denormalised channel-prompt view) means a ToolCall-node approval's
    resume-time record now carries policy_id/approval_type/gate_reason,
    which pending_dispatch entries never stored at all."""
    storage_provider = _FakeStorageProvider()
    pool = SimpleNamespace(_storage=storage_provider)
    session_id = "graph-rec-2"
    checkpoint = _two_gate_checkpoint(session_id)

    await write_approval_record_for_graph(
        pool, session=_session(session_id), checkpoint=checkpoint,
        tcid="call-0", payload={"decision": "rejected", "reason": "no"},
    )

    items = await _records_for(storage_provider)
    assert len(items) == 1
    assert items[0].policy_id == "pol-fanout"
    assert items[0].approval_type == "required"
    assert items[0].gate_reason == "matched policy"
    assert items[0].reason == "no"


@pytest.mark.asyncio
async def test_resume_record_skipped_for_unknown_tcid():
    storage_provider = _FakeStorageProvider()
    pool = SimpleNamespace(_storage=storage_provider)
    session_id = "graph-rec-3"
    checkpoint = _two_gate_checkpoint(session_id)

    await write_approval_record_for_graph(
        pool, session=_session(session_id), checkpoint=checkpoint,
        tcid="call-nonexistent", payload={"decision": "approved"},
    )

    assert await _records_for(storage_provider) == []


@pytest.mark.asyncio
async def test_resume_record_skipped_when_tcid_is_none():
    """The legacy single-event drain-all path can resume with tcid=None;
    there is no specific gate to attribute a record to, so none is
    written (matches the pre-fix behaviour for this case)."""
    storage_provider = _FakeStorageProvider()
    pool = SimpleNamespace(_storage=storage_provider)
    session_id = "graph-rec-4"
    checkpoint = _two_gate_checkpoint(session_id)

    await write_approval_record_for_graph(
        pool, session=_session(session_id), checkpoint=checkpoint,
        tcid=None, payload={"decision": "approved"},
    )

    assert await _records_for(storage_provider) == []


@pytest.mark.asyncio
async def test_resume_record_dedupes_against_a_respond_time_write_for_the_same_gate(
    tmp_path,
):
    """01a068da's idempotency guarantee extended to the graph path: if
    the respond route already wrote call-1's record (the normal case --
    this resume-time write is a fallback), the resume must not add a
    second row. Uses a REAL SqliteStorageProvider: the shared
    _FakeStorageProvider only enforces uniqueness on the primary key, so
    it would silently let a duplicate gate_event_key through and this
    test would prove nothing (same gap test_approval_record_resume.py's
    dedupe test guards against on the agent path).
    """
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    session_id = "graph-rec-dedupe"
    checkpoint = _two_gate_checkpoint(session_id)
    gate_key = f"tool_approval:{session_id}:call-1"

    storage_provider = SqliteStorageProvider(
        SqliteConfig(path=str(tmp_path / "dedupe.sqlite")),
    )
    await storage_provider.initialize()
    try:
        preseeded = ToolApprovalRecord(
            tool_name="delete_workspace",
            arguments={"id": "ws-worker[1]"},
            tool_call_id="call-1",
            session_id=session_id,
            agent_id="agt-graph",
            decided_at=datetime.now(timezone.utc),
            decision="approved",
            gate_event_key=gate_key,
        )
        await storage_provider.get_storage(ToolApprovalRecord).create(preseeded)

        pool = SimpleNamespace(_storage=storage_provider)
        await write_approval_record_for_graph(
            pool, session=_session(session_id), checkpoint=checkpoint,
            tcid="call-1", payload={"decision": "approved"},
        )

        items = await _records_for(storage_provider)
        assert len(items) == 1
        assert items[0].id == preseeded.id
    finally:
        await storage_provider.aclose()
