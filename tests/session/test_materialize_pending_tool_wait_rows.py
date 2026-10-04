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
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState, tool_call_task_id
from primer.model.workspace_session import (
    AgentSessionBinding, SessionStatus, WorkspaceSession,
)
from primer.session.persistence import (
    _CoalesceState,
    _create_tool_call_task_idempotent,
    materialize_pending_tool_wait_rows,
)

from tests.conftest import _FakeStorageProvider


class _RecordingClaimEngine:
    def __init__(self) -> None:
        self.upserted: list[tuple[ClaimKind, str]] = []
        self.priorities: dict[tuple[ClaimKind, str], object] = {}

    async def upsert(self, kind, entity_id, **kwargs) -> None:
        self.upserted.append((kind, entity_id))
        self.priorities[(kind, entity_id)] = kwargs.get("priority")


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
        # the provider's raw id of each call, as the graph executor stamps it
        "call_ids": {i: f"raw_{i}" for i in [*outstanding, *notifying]},
    }


def _q(scoped_id: str) -> str:
    """The session-qualified row id of ``scoped_id`` in session ``s1`` (S1b)."""
    return tool_call_task_id("s1", scoped_id)


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

    task_a = await task_storage.get(_q("A:tool:0:1"))
    task_b = await task_storage.get(_q("B:tool:0:1"))
    assert task_a.state == ToolCallTaskState.QUEUED
    assert task_a.batch_task_ids == [_q("A:tool:0:1")]
    assert task_b.batch_task_ids == [_q("B:tool:0:1")]
    assert (task_a.call_id, task_b.call_id) == ("raw_A:tool:0:1", "raw_B:tool:0:1")
    assert (task_a.scoped_call_id, task_b.scoped_call_id) == ("A:tool:0:1", "B:tool:0:1")
    assert wake_keys == ["tool_wait:s1:0:A", "tool_wait:s1:0:B"]
    assert set(claim_engine.upserted) == {
        (ClaimKind.TOOL_CALL, _q("A:tool:0:1")), (ClaimKind.TOOL_CALL, _q("B:tool:0:1")),
    }
    # Armed at the RESUME priority (50), never the fresh-work default: a tool call continues a turn a
    # human is waiting on and must not queue behind fresh sessions.
    from primer.int.claim import CLAIM_PRIORITY_RESUME
    assert set(claim_engine.priorities.values()) == {CLAIM_PRIORITY_RESUME}


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

    task = await task_storage.get(_q("A:tool:0:2"))
    assert task.state == ToolCallTaskState.DONE
    assert task.result_state == {"id": "A:tool:0:2", "output": "inline", "error": False}
    assert task.call_id == "raw_A:tool:0:2"
    # batch_task_ids on the notifying row still carries the FULL node
    # batch (claimable + notifying), not just itself.
    assert task.batch_task_ids == [_q("A:tool:0:1"), _q("A:tool:0:2")]
    # Only the claimable id gets a claim-engine lease.
    assert claim_engine.upserted == [(ClaimKind.TOOL_CALL, _q("A:tool:0:1"))]


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
    assert await task_storage.get(_q("A:tool:0:1")) is not None
    assert await task_storage.get(_q("B:tool:0:1")) is None


def _task(*, record_seq: int) -> ToolCallTask:
    return ToolCallTask(
        id=_q("A:tool:0:1"), session_id="s1", turn_no=0, tool_name="t",
        state=ToolCallTaskState.QUEUED, record_seq=record_seq,
        created_at=datetime.now(timezone.utc), batch_task_ids=[_q("A:tool:0:1")],
    )


@pytest.mark.asyncio
async def test_strict_true_raises_on_record_seq_mismatch() -> None:
    """The doctrine's existing behavior, unchanged: at the live-turn park
    catches (strict=True, the default), a crash-retry's re-run mints the
    IDENTICAL record_seq deterministically - a MISMATCH means something
    else created a conflicting row, and must raise loudly."""
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    await task_storage.create(_task(record_seq=1))

    with pytest.raises(RuntimeError, match="not a crash-retry replay"):
        await _create_tool_call_task_idempotent(
            task_storage, _task(record_seq=2), session_id="s1",
        )


@pytest.mark.asyncio
async def test_strict_false_treats_existing_row_as_replay_without_comparing_record_seq() -> None:
    """7a gate review (verdict R3-3): at the graph-resume repark call site
    (strict=False), the crash-retry doctrine's "record_seq must match"
    precondition is FALSE - persist_resume_tool_result_record_for_graph
    durably advances last_seq BEFORE the drain runs, so a genuine
    crash-then-retry computes a DIFFERENT record_seq for the SAME scoped
    id on retry. Comparing record_seq there would raise on every
    crash-retry, and the caller maps that into _end_session(failed) - a
    crash-retry must not kill the session. strict=False treats ANY
    existing row for this scoped id as a replay, record_seq mismatch or
    not."""
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    await task_storage.create(_task(record_seq=1))

    # Must NOT raise, despite the mismatched record_seq.
    await _create_tool_call_task_idempotent(
        task_storage, _task(record_seq=2), session_id="s1", strict=False,
    )

    # The original row is untouched - this is a no-op, not an update.
    existing = await task_storage.get(_q("A:tool:0:1"))
    assert existing.record_seq == 1


@pytest.mark.asyncio
async def test_the_entries_are_stored_back_in_the_qualified_form() -> None:
    """The dict inside the checkpoint is what gets parked: after materialization it names the rows by the id they
    have, so a resume reading the blob finds them (and a carried-over, already qualified entry passes unchanged)."""
    storage_provider = _FakeStorageProvider()
    session = _session()
    cs = _CoalesceState()
    for tid in ("A:tool:0:1", "A:tool:0:2"):
        cs.tool_call_record_seq[tid] = 1
        cs.tool_call_record_name[tid] = "t"
    entry = _pending_tool_wait("A", ["A:tool:0:1"], notifying=("A:tool:0:2",))
    carried = _pending_tool_wait("B", [_q("B:tool:0:1")])

    await materialize_pending_tool_wait_rows(
        storage_provider, None, session.id, session.turn_no, cs, datetime.now(timezone.utc),
        [entry, carried], strict=False,
    )

    assert entry["outstanding_task_ids"] == [_q("A:tool:0:1")]
    assert [i for i, _ in entry["notifying_results"]] == [_q("A:tool:0:2")]
    assert carried["outstanding_task_ids"] == [_q("B:tool:0:1")], "an already qualified id is left alone"

    again = [dict(entry)]
    await materialize_pending_tool_wait_rows(
        storage_provider, None, session.id, session.turn_no, cs, datetime.now(timezone.utc),
        again, strict=False,
    )
    assert again[0]["outstanding_task_ids"] == [_q("A:tool:0:1")], "qualifying twice does not double the prefix"


@pytest.mark.asyncio
async def test_two_sessions_materializing_the_same_scoped_batch_get_separate_rows_and_leases() -> None:
    """The bug S1b closes: every agent session's first call of turn 0 is `x:tool:0:1`, and a row id is a global
    primary key. Two sessions must not share a row, a batch list or a lease."""
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    claim_engine = _RecordingClaimEngine()
    for session_id, result_text in (("sess-A", "inline A"), ("sess-B", "inline B")):
        cs = _CoalesceState()
        for tid in ("x:tool:0:1", "x:tool:0:2"):
            cs.tool_call_record_seq[tid] = 1
            cs.tool_call_record_name[tid] = "t"
        entry = {
            "node_id": "x", "outstanding_task_ids": ["x:tool:0:1"],
            "notifying_results": [("x:tool:0:2", {"id": "raw2", "output": result_text, "error": False})],
            "call_ids": {"x:tool:0:1": "raw1", "x:tool:0:2": "raw2"},
        }
        await materialize_pending_tool_wait_rows(
            storage_provider, claim_engine, session_id, 0, cs, datetime.now(timezone.utc), [entry],
        )

    a = await task_storage.get("sess-A/x:tool:0:2")
    b = await task_storage.get("sess-B/x:tool:0:2")
    assert (a.session_id, b.session_id) == ("sess-A", "sess-B")
    assert (a.result_state["output"], b.result_state["output"]) == ("inline A", "inline B")
    assert a.batch_task_ids == ["sess-A/x:tool:0:1", "sess-A/x:tool:0:2"]
    assert b.batch_task_ids == ["sess-B/x:tool:0:1", "sess-B/x:tool:0:2"]
    assert set(claim_engine.upserted) == {
        (ClaimKind.TOOL_CALL, "sess-A/x:tool:0:1"), (ClaimKind.TOOL_CALL, "sess-B/x:tool:0:1"),
    }


@pytest.mark.asyncio
async def test_a_row_of_another_session_under_the_same_id_is_never_adopted_as_a_replay() -> None:
    """Ids are session-qualified, so this is unreachable by construction; the guard is for a bug that makes it
    reachable. Adopting the row (record_seq happening to match) would hand this session that session's result."""
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    other = _task(record_seq=1).model_copy(update={"session_id": "someone-else"})
    await task_storage.create(other)

    for strict in (True, False):
        with pytest.raises(RuntimeError, match="cross-session id collision"):
            await _create_tool_call_task_idempotent(
                task_storage, _task(record_seq=1), session_id="s1", strict=strict,
            )
