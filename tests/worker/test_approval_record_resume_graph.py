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


def _mixed_agent_yields_checkpoint(session_id: str) -> dict:
    """An AGENT node's own approval gate (``tool_name="_approval"``) and
    an AGENT node's ask_user yield, BOTH in ``pending_agent_yields``,
    nothing in ``pending_toolcalls``.

    01a06b82 gate-review R2: the ``pending_agent_yields`` arm of
    ``enumerate_pending_gates`` (which maps its own ``event_key`` field)
    had zero test coverage -- every fixture elsewhere in this branch put
    its approval gate in ``pending_toolcalls`` instead. An agent node's
    OWN gated tool call lands here (tool_manager.py fires the identical
    approval yield whether the caller is a graph ToolCall node or an
    agent node's own LLM-driven tool call), and this gate type DOES get
    a node-scoped key (agent-node dispatch is wrapped in
    ``set_current_graph_node_id``, unlike a ToolCall node's).
    """
    approval_key = f"tool_approval:{session_id}:worker[0]:call-approve"
    ask_key = f"ask_user:{session_id}:worker[1]:call-ask"
    return {
        "pending_toolcalls": [],
        "pending_agent_yields": [
            {
                "node_id": "worker[0]",
                "tool_call_id": "call-approve",
                "event_key": approval_key,
                "tool_name": "_approval",
                "resume_metadata": {
                    "policy_id": "pol-agent",
                    "approval_type": "required",
                    "gate_reason": "matched policy",
                    "approvers": None,
                    "original_call": {
                        "id": "call-approve", "name": "delete_workspace",
                        "arguments": {"id": "ws-x"},
                    },
                },
            },
            {
                "node_id": "worker[1]",
                "tool_call_id": "call-ask",
                "event_key": ask_key,
                "tool_name": "ask_user",
                "resume_metadata": {"prompt": "color?"},
            },
        ],
        "pending_dispatch": [],
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
async def test_resume_record_resolves_an_approval_gate_from_pending_agent_yields():
    """01a06b82 gate-review R2: an approval gate living in
    pending_agent_yields (not pending_toolcalls) must resolve correctly,
    with its own event_key and full policy metadata."""
    storage_provider = _FakeStorageProvider()
    pool = SimpleNamespace(_storage=storage_provider)
    session_id = "graph-rec-mixed"
    checkpoint = _mixed_agent_yields_checkpoint(session_id)

    await write_approval_record_for_graph(
        pool, session=_session(session_id), checkpoint=checkpoint,
        tcid="call-approve", payload={"decision": "approved"},
    )

    items = await _records_for(storage_provider)
    assert len(items) == 1
    rec = items[0]
    assert rec.tool_call_id == "call-approve"
    assert rec.decision == "approved"
    assert rec.policy_id == "pol-agent"
    assert rec.gate_event_key == f"tool_approval:{session_id}:worker[0]:call-approve"


@pytest.mark.asyncio
async def test_resume_record_skipped_for_the_ask_user_tcid_in_a_mixed_checkpoint():
    """write_approval_record_for_graph must never write an approval
    record for an ask_user-kind entry, even when its tcid resolves to
    something in the checkpoint (the coexisting pending_agent_yields
    entry) -- the kind="_approval" filter must hold across both arms."""
    storage_provider = _FakeStorageProvider()
    pool = SimpleNamespace(_storage=storage_provider)
    session_id = "graph-rec-mixed2"
    checkpoint = _mixed_agent_yields_checkpoint(session_id)

    await write_approval_record_for_graph(
        pool, session=_session(session_id), checkpoint=checkpoint,
        tcid="call-ask", payload={"decision": "approved"},
    )

    assert await _records_for(storage_provider) == []


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


@pytest.mark.asyncio
async def test_resume_record_warns_on_a_real_vs_real_decision_disagreement(
    tmp_path, caplog,
):
    """01a07be5 gate-review-2 finding 2: warn_on_decision_mismatch is now
    unconditional, not just for a synthesised timeout/cancel. The
    flip-winner (whichever publish actually resumed this park) and the
    record-winner (whichever write_approval_record call wins the
    gate_event_key race) are independent races even for two REAL operator
    decisions -- a respond-time writer can lose the flip but still win
    the record race with a DIFFERENT decision than the one that actually
    resumed the park. This resume-time write knows the TRUE resumed
    decision (the payload it was actually called with), so a losing
    ConflictError against a DISAGREEING real decision must be loud now,
    not silently swallowed at DEBUG the way it would have been when the
    check only fired for terminal synthesis.
    """
    import logging

    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    session_id = "graph-rec-real-disagree"
    checkpoint = _two_gate_checkpoint(session_id)
    gate_key = f"tool_approval:{session_id}:call-1"

    storage_provider = SqliteStorageProvider(
        SqliteConfig(path=str(tmp_path / "real-disagree.sqlite")),
    )
    await storage_provider.initialize()
    try:
        # A DIFFERENT writer already recorded "rejected" for this gate.
        preseeded = ToolApprovalRecord(
            tool_name="delete_workspace",
            arguments={"id": "ws-worker[1]"},
            tool_call_id="call-1",
            session_id=session_id,
            agent_id="agt-graph",
            decided_at=datetime.now(timezone.utc),
            decision="rejected",
            gate_event_key=gate_key,
        )
        await storage_provider.get_storage(ToolApprovalRecord).create(preseeded)

        pool = SimpleNamespace(_storage=storage_provider)
        # This resume-time write reflects the REAL decision that actually
        # resumed the park -- "approved" -- a genuine disagreement with
        # the preseeded "rejected", NOT a terminal synthesis.
        with caplog.at_level(
            logging.DEBUG, logger="primer.agent.approval_record",
        ):
            await write_approval_record_for_graph(
                pool, session=_session(session_id), checkpoint=checkpoint,
                tcid="call-1", payload={"decision": "approved"},
            )

        # Append-only still holds: the preseeded row is untouched.
        items = await _records_for(storage_provider)
        assert len(items) == 1
        assert items[0].id == preseeded.id
        assert items[0].decision == "rejected"

        error_records = [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert len(error_records) == 1
        message = error_records[0].getMessage()
        assert "audit disagreement" in message
        assert "rejected" in message
        assert "approved" in message
    finally:
        await storage_provider.aclose()


@pytest.mark.asyncio
async def test_resume_record_resolves_a_nested_approval_leaf_with_frames_populated():
    """01a07be5 gate-review-2 finding 1: the pending_agent_yields arm's
    resolution must be identical whether or not the entry carries a
    nested continuation frames/leaf stack -- resolve_pending_gate never
    inspects those fields, only the entry's own top-level tool_name/
    tool_call_id/event_key/resume_metadata (which are always the LEAF's
    own values regardless of nesting, per _node_dispatch.py's
    construction). This is the exact entry shape resume_graph_engine's
    nested branch now writes a record for, which nothing exercised
    before this round.
    """
    from primer.worker.frames import frames_to_jsonable
    from primer.worker.frames import AgentFrame, AgentResumeContext

    session_id = "graph-rec-nested"
    tcid = "call-nested"
    event_key = f"tool_approval:{session_id}:worker[0]:{tcid}"
    resume_metadata = {
        "policy_id": "pol-nested",
        "approval_type": "required",
        "gate_reason": "matched policy",
        "approvers": None,
        "original_call": {
            "id": tcid, "name": "delete_workspace", "arguments": {},
        },
    }
    frame = AgentFrame(
        agent_id="sub",
        llm_messages=[{"role": "assistant", "parts": []}],
        tool_call_id="invoke-tc",
        depth=0,
        context=AgentResumeContext(
            session_id=session_id, workspace_id="w", chat_id=None,
            principal="p", tools=["misc__gated_tool"],
        ),
    )
    checkpoint = {
        "pending_toolcalls": [],
        "pending_agent_yields": [{
            "node_id": "worker[0]",
            "tool_call_id": tcid,
            "event_key": event_key,
            "tool_name": "_approval",
            "resume_metadata": resume_metadata,
            "llm_messages": [],
            "iteration": 0,
            "frames": frames_to_jsonable([frame]),
            "leaf": {
                "tool_name": "_approval", "event_key": event_key,
                "resume_metadata": resume_metadata,
            },
        }],
        "pending_dispatch": [],
    }

    storage_provider = _FakeStorageProvider()
    pool = SimpleNamespace(_storage=storage_provider)

    await write_approval_record_for_graph(
        pool, session=_session(session_id), checkpoint=checkpoint,
        tcid=tcid, payload={"decision": "approved"},
    )

    items = await _records_for(storage_provider)
    assert len(items) == 1
    rec = items[0]
    assert rec.tool_call_id == tcid
    assert rec.decision == "approved"
    assert rec.policy_id == "pol-nested"
    assert rec.gate_event_key == event_key
