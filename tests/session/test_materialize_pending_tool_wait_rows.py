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
from primer.model.except_ import ConflictError
from primer.session.persistence import (
    TurnInvariantError,
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
    placeholder tool_name. Loudly means the exact ``TurnInvariantError``:
    that, not its ``RuntimeError`` parent, is what the live-turn park arms
    end the session failed on."""
    storage_provider = _FakeStorageProvider()
    session = _session()
    with pytest.raises(TurnInvariantError, match="outstanding task 'A:tool:0:1' has no matching TOOL_CALL") as ei:
        await materialize_pending_tool_wait_rows(
            storage_provider, None, session.id, session.turn_no,
            _CoalesceState(), datetime.now(timezone.utc),
            [_pending_tool_wait("A", ["A:tool:0:1"])],
        )
    assert type(ei.value) is TurnInvariantError


@pytest.mark.asyncio
async def test_a_notifying_result_with_no_coalesce_record_raises_the_exact_invariant_class() -> None:
    """The notifying half of the same invariant: the claimable call has its record, the inline-answered one does
    not."""
    storage_provider = _FakeStorageProvider()
    session = _session()
    cs = _CoalesceState()
    cs.tool_call_record_seq["A:tool:0:1"] = 1
    cs.tool_call_record_name["A:tool:0:1"] = "t"
    with pytest.raises(TurnInvariantError, match="notifying result 'A:tool:0:2' has no matching TOOL_CALL") as ei:
        await materialize_pending_tool_wait_rows(
            storage_provider, None, session.id, session.turn_no, cs, datetime.now(timezone.utc),
            [_pending_tool_wait("A", ["A:tool:0:1"], notifying=("A:tool:0:2",))],
        )
    assert type(ei.value) is TurnInvariantError


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


class _RowGoneOnReadStorage:
    """Refuses the create as a duplicate, then finds nothing on the read: the row lost a race with a delete."""

    async def create(self, entity, **kwargs):
        raise ConflictError(f"id {entity.id!r} already exists")

    async def get(self, entity_id, **kwargs):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [True, False])
async def test_a_row_gone_on_read_raises_the_plain_runtime_error_not_the_invariant_class(strict) -> None:
    """Not deterministic (a re-run's create can succeed), so not a reason to end the turn: it must stay the plain
    ``RuntimeError``, which the park arms let propagate instead of ending the session."""
    with pytest.raises(RuntimeError, match="record_seq=<gone>") as ei:
        await _create_tool_call_task_idempotent(
            _RowGoneOnReadStorage(), _task(record_seq=1), session_id="s1", strict=strict,
        )
    assert type(ei.value) is RuntimeError


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


@pytest.mark.asyncio
async def test_a_skipped_legacy_bare_entry_keeps_its_stored_ids_so_its_rows_stay_findable() -> None:
    """A development flag-on park written before ids were qualified has its rows under the BARE id. On a partial-wake
    re-park (``strict=False``) that carried-over entry is skipped (this resume never minted it), and rewriting it to a
    qualified id its rows do not have made the next wake find nothing and end the session failed. A skipped bare id
    keeps the form it was stored in; an entry this call materialized, and a carried-over entry that is already
    qualified, end up qualified; the wake keys do not depend on the form."""
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    session = _session()
    cs = _CoalesceState()
    for tid in ("N:tool:0:1", "N:tool:0:2"):
        cs.tool_call_record_seq[tid] = 1
        cs.tool_call_record_name[tid] = "t"
    legacy = _pending_tool_wait("L", ["L:tool:0:1"], notifying=("L:tool:0:2",))      # bare, never minted by this resume
    fresh = _pending_tool_wait("N", ["N:tool:0:1"], notifying=("N:tool:0:2",))       # minted by this resume
    qualified = _pending_tool_wait("Q", [_q("Q:tool:0:1")])                           # carried over, already qualified

    wake_keys = await materialize_pending_tool_wait_rows(
        storage_provider, None, session.id, session.turn_no, cs, datetime.now(timezone.utc),
        [legacy, fresh, qualified], strict=False,
    )

    assert legacy["outstanding_task_ids"] == ["L:tool:0:1"], "a skipped legacy id was rewritten to a form its row does not have"
    assert [i for i, _ in legacy["notifying_results"]] == ["L:tool:0:2"]
    assert fresh["outstanding_task_ids"] == [_q("N:tool:0:1")]
    assert [i for i, _ in fresh["notifying_results"]] == [_q("N:tool:0:2")]
    assert qualified["outstanding_task_ids"] == [_q("Q:tool:0:1")]
    assert wake_keys == ["tool_wait:s1:0:L", "tool_wait:s1:0:N", "tool_wait:s1:0:Q"]
    assert await task_storage.get(_q("N:tool:0:1")) is not None
    assert await task_storage.get(_q("L:tool:0:1")) is None, "the skipped entry must not gain rows"


@pytest.mark.asyncio
async def test_a_wake_key_takes_the_turn_of_the_id_not_the_turn_argument() -> None:
    """The ``turn_no`` argument stamps the rows; the wake key comes from the id alone, so a carried-over batch keeps
    the key it was parked under after the session's turn moved on (mutation N27, call-site leg)."""
    storage_provider = _FakeStorageProvider()
    cs = _CoalesceState()
    cs.tool_call_record_seq["a:b:tool:3:1"] = 1
    cs.tool_call_record_name["a:b:tool:3:1"] = "t"

    wake_keys = await materialize_pending_tool_wait_rows(
        storage_provider, None, "s1", 5, cs, datetime.now(timezone.utc),
        [_pending_tool_wait("a:b", ["a:b:tool:3:1"])],
    )

    assert wake_keys == ["tool_wait:s1:3:a:b"]
    row = await storage_provider.get_storage(ToolCallTask).get(_q("a:b:tool:3:1"))
    assert row.turn_no == 5


@pytest.mark.asyncio
async def test_a_malformed_batch_drops_only_its_own_key_and_its_rows_are_still_created(caplog) -> None:
    """A batch whose ids do not parse has no wake key. The materializer never guesses one: it logs ERROR, counts it,
    and returns the other batches' keys; row creation is unchanged. All batches malformed returns no key and does not
    raise here (the mixed park still has its human gate's key; the pure park arms decide)."""
    import logging

    import primer.observability.metrics as metrics

    metrics.reset_for_test()
    storage_provider = _FakeStorageProvider()
    task_storage = storage_provider.get_storage(ToolCallTask)
    cs = _CoalesceState()
    for tid in ("A:tool:0:1", "B:tool:03:1", "C:tool:+1:1"):
        cs.tool_call_record_seq[tid] = 1
        cs.tool_call_record_name[tid] = "t"

    with caplog.at_level(logging.ERROR):
        wake_keys = await materialize_pending_tool_wait_rows(
            storage_provider, None, "s1", 0, cs, datetime.now(timezone.utc),
            [_pending_tool_wait("B", ["B:tool:03:1"]), _pending_tool_wait("A", ["A:tool:0:1"])],
        )
    assert wake_keys == ["tool_wait:s1:0:A"]
    assert await task_storage.get(_q("B:tool:03:1")) is not None, "the malformed batch's row was not created"
    assert metrics.tool_wait_malformed_scoped_id_total.labels("materializer")._value.get() == 1.0
    assert any(r.levelno == logging.ERROR and repr(_q("B:tool:03:1")) in r.getMessage() for r in caplog.records)

    assert await materialize_pending_tool_wait_rows(
        storage_provider, None, "s1", 0, cs, datetime.now(timezone.utc),
        [_pending_tool_wait("C", ["C:tool:+1:1"])],
    ) == []
    assert metrics.tool_wait_malformed_scoped_id_total.labels("materializer")._value.get() == 2.0
