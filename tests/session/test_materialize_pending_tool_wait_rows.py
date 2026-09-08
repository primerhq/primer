"""``materialize_pending_tool_wait_rows`` (Phase 3 stage 7a, 01a0518b
boundary d) - direct unit tests for the graph third-list's shared
row-creation helper. Exercised end-to-end by the graph order tests, but
its own per-node scoping contract (batch_task_ids scoped to THAT node
only, wake keys derived per node, claim registration for
outstanding-only ids) had no direct test of its own.

7a gate review (verdict R2-1): relocated from ``primer.session.dispatch``
(a private helper there) to ``primer.session.persistence`` (a shared
helper both dispatch.py's park catches and the graph-resume repark path
call) - these tests now call it directly, without the ``deps``/
``SessionDispatchDeps`` bundle the dispatch-local version needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding, SessionStatus, WorkspaceSession,
)
from primer.session.persistence import _CoalesceState, materialize_pending_tool_wait_rows

from tests.conftest import _FakeStorageProvider


class _RecordingClaimEngine:
    def __init__(self) -> None:
        self.upserted: list[tuple[ClaimKind, str]] = []

    async def upsert(self, kind, entity_id, **kwargs) -> None:
        self.upserted.append((kind, entity_id))


def _session() -> WorkspaceSession:
    return WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), turn_no=0,
    )


def _pending_tool_wait(node_id: str, outstanding: list[str], notifying: tuple[str, ...] = ()) -> dict:
    return {
        "node_id": node_id,
        "outstanding_task_ids": outstanding,
        "notifying_results": [
            (nid, {"id": nid, "output": "inline", "error": False}) for nid in notifying
        ],
    }


@pytest.mark.asyncio
async def test_creates_per_node_scoped_batch_task_ids() -> None:
    """Two co-pending nodes' batches must NOT share batch_task_ids or a
    wake key - the whole point of the per-node materialization (versus
    the pure-branch's own flattened fields, which would combine them)."""
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    session = _session()
    cs = _CoalesceState()
    for tid in ("A:tool:0:1", "B:tool:0:1"):
        cs.tool_call_record_seq[tid] = 1
        cs.tool_call_record_name[tid] = "t"

    claim_engine = _RecordingClaimEngine()

    wake_keys = await materialize_pending_tool_wait_rows(
        storage_provider, claim_engine, session.id, session.turn_no,
        cs, datetime.now(timezone.utc),
        [
            _pending_tool_wait("A", ["A:tool:0:1"]),
            _pending_tool_wait("B", ["B:tool:0:1"]),
        ],
    )

    task_a = await task_storage.get("A:tool:0:1")
    task_b = await task_storage.get("B:tool:0:1")
    assert task_a.state == ToolCallTaskState.QUEUED
    assert task_a.batch_task_ids == ["A:tool:0:1"]
    assert task_b.batch_task_ids == ["B:tool:0:1"]
    assert wake_keys == ["tool_wait:s1:0:A", "tool_wait:s1:0:B"]
    assert set(claim_engine.upserted) == {
        (ClaimKind.TOOL_CALL, "A:tool:0:1"), (ClaimKind.TOOL_CALL, "B:tool:0:1"),
    }


@pytest.mark.asyncio
async def test_notifying_result_creates_terminal_row_no_claim_upsert() -> None:
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    session = _session()
    cs = _CoalesceState()
    for tid in ("A:tool:0:1", "A:tool:0:2"):
        cs.tool_call_record_seq[tid] = 1
        cs.tool_call_record_name[tid] = "t"

    claim_engine = _RecordingClaimEngine()

    await materialize_pending_tool_wait_rows(
        storage_provider, claim_engine, session.id, session.turn_no,
        cs, datetime.now(timezone.utc),
        [_pending_tool_wait("A", ["A:tool:0:1"], notifying=("A:tool:0:2",))],
    )

    task = await task_storage.get("A:tool:0:2")
    assert task.state == ToolCallTaskState.DONE
    assert task.result_state == {"id": "A:tool:0:2", "output": "inline", "error": False}
    # batch_task_ids on the notifying row still carries the FULL node
    # batch (claimable + notifying), not just itself.
    assert task.batch_task_ids == ["A:tool:0:1", "A:tool:0:2"]
    # Only the claimable id gets a claim-engine lease.
    assert claim_engine.upserted == [(ClaimKind.TOOL_CALL, "A:tool:0:1")]


@pytest.mark.asyncio
async def test_empty_pending_tool_waits_is_a_noop() -> None:
    storage_provider = _FakeStorageProvider()
    session = _session()
    wake_keys = await materialize_pending_tool_wait_rows(
        storage_provider, None, session.id, session.turn_no,
        _CoalesceState(), datetime.now(timezone.utc), [],
    )
    assert wake_keys == []


@pytest.mark.asyncio
async def test_missing_coalesce_record_raises_loudly() -> None:
    """The durable-append-before-claimable invariant: a scoped id with no
    matching TOOL_CALL record in this turn's coalesce_state must fail
    loudly (the default ``strict=True``), never silently mint a
    placeholder tool_name."""
    storage_provider = _FakeStorageProvider()
    session = _session()
    with pytest.raises(RuntimeError, match="no matching TOOL_CALL record"):
        await materialize_pending_tool_wait_rows(
            storage_provider, None, session.id, session.turn_no,
            _CoalesceState(), datetime.now(timezone.utc),
            [_pending_tool_wait("A", ["A:tool:0:1"])],
        )


@pytest.mark.asyncio
async def test_non_strict_mode_skips_unminted_entries_instead_of_raising() -> None:
    """7a gate review (verdict R2-1): the repark call site passes
    ``strict=False`` since its ``pending_tool_waits`` can be a MIX of
    entries carried over untouched from an EARLIER park (this resume's
    own coalesce_state never observed them) and genuinely NEW entries
    this resume's own dispatch just produced. An untouched entry must be
    silently skipped, not treated as an invariant violation - but its
    wake key is still returned (a pure function of the ids, unaffected
    by whether the coalesce_state knows about them)."""
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    session = _session()
    cs = _CoalesceState()
    cs.tool_call_record_seq["A:tool:0:1"] = 1
    cs.tool_call_record_name["A:tool:0:1"] = "t"
    # B's entry is NOT in cs at all - simulates a carried-over batch this
    # resume's own drain never touched.

    wake_keys = await materialize_pending_tool_wait_rows(
        storage_provider, None, session.id, session.turn_no,
        cs, datetime.now(timezone.utc),
        [
            _pending_tool_wait("A", ["A:tool:0:1"]),
            _pending_tool_wait("B", ["B:tool:0:1"]),
        ],
        strict=False,
    )

    assert wake_keys == ["tool_wait:s1:0:A", "tool_wait:s1:0:B"]
    assert await task_storage.get("A:tool:0:1") is not None
    assert await task_storage.get("B:tool:0:1") is None
