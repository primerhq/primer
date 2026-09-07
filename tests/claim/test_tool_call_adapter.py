"""Unit tests for ToolCallClaimAdapter (Phase 3 stage 7a, 01a0518b).

Mirrors tests/claim/test_session_adapter.py's shape (eligibility_sql,
entity_indexes, on_release scenarios via a FakeStorage double) - the
established pattern for this test class.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.int.claim import ClaimKind, ParkRequest, PostReleaseWake, ReleaseOutcome
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_task(
    task_id: str,
    *,
    state: ToolCallTaskState = ToolCallTaskState.RUNNING,
    gate_state: dict | None = None,
    batch_task_ids: list[str] | None = None,
) -> ToolCallTask:
    return ToolCallTask(
        id=task_id,
        session_id="sess-1",
        turn_no=0,
        tool_name="workspace__write",
        state=state,
        record_seq=1,
        gate_state=gate_state,
        batch_task_ids=batch_task_ids or [],
        created_at=_now(),
        started_at=_now(),
    )


class FakeStorage:
    def __init__(self, task: ToolCallTask) -> None:
        self._task = task
        self.updated: list[ToolCallTask] = []

    async def get(self, id: str, *, conn=None) -> ToolCallTask | None:
        return self._task if self._task.id == id else None

    async def update(self, entity: ToolCallTask, *, conn=None) -> ToolCallTask:
        self.updated.append(entity)
        self._task = entity
        return entity


class FakeMultiStorage:
    """Sibling-check tests need more than one row - keyed by id, tracks
    the sequence of ``get`` calls so the "reads via conn, not find()"
    doctrine is independently checkable if ever needed."""

    def __init__(self, tasks: list[ToolCallTask]) -> None:
        self._tasks = {t.id: t for t in tasks}
        self.updated: list[ToolCallTask] = []
        self.get_calls: list[str] = []

    async def get(self, id: str, *, conn=None) -> ToolCallTask | None:
        self.get_calls.append(id)
        return self._tasks.get(id)

    async def update(self, entity: ToolCallTask, *, conn=None) -> ToolCallTask:
        self.updated.append(entity)
        self._tasks[entity.id] = entity
        return entity


# ---------------------------------------------------------------------------
# Kind / eligibility / indexes
# ---------------------------------------------------------------------------


def test_tool_call_adapter_kind():
    a = ToolCallClaimAdapter(task_storage=None)
    assert a.kind is ClaimKind.TOOL_CALL
    assert a.entity_table == "toolcalltask"


def test_tool_call_eligibility_sql():
    a = ToolCallClaimAdapter(task_storage=None)
    sql = a.eligibility_sql()
    # state lives in the JSONB data column - a bare e.state reference
    # raises UndefinedColumnError on Postgres and breaks the WHOLE claim
    # loop, not just this kind (same footgun the session adapter's own
    # test guards against).
    assert "e.data->>'state'" in sql
    assert "e.state" not in sql
    assert "'queued'" in sql
    assert "'running'" in sql
    # Excluded: gated (task-granular gating - the whole point) and both
    # terminal states.
    assert "'gated'" not in sql
    assert "'done'" not in sql
    assert "'failed'" not in sql


def test_tool_call_entity_indexes_are_safe_to_repeat():
    a = ToolCallClaimAdapter(task_storage=None)
    ddl = a.entity_indexes('"public"."toolcalltask"')
    assert ddl, "expected at least one index statement"
    assert all(d.startswith("CREATE INDEX IF NOT EXISTS") for d in ddl)
    assert all('"public"."toolcalltask"' in d for d in ddl)
    joined = "\n".join(ddl)
    assert "(data->>'state')" in joined
    assert "(data->>'session_id')" in joined
    assert "(data->>'turn_no')" in joined


# ---------------------------------------------------------------------------
# on_release: gate branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_gate_sets_gated_state_and_event_key() -> None:
    task = _make_task("t1")
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    until = _now()
    await adapter.on_release(
        conn=None,
        entity_id="t1",
        outcome=ReleaseOutcome(
            success=False,
            drop_lease=True,
            park=ParkRequest(
                parked_state={"kind": "approval"},
                parked_event_key="tool_approval:sess-1:t1",
                parked_until=until,
                parked_at=_now(),
            ),
        ),
    )
    assert len(storage.updated) == 1
    updated = storage.updated[0]
    assert updated.state == ToolCallTaskState.GATED
    assert updated.gate_event_key == "tool_approval:sess-1:t1"
    assert updated.gate_until == until
    assert updated.gate_state == {"kind": "approval"}


# ---------------------------------------------------------------------------
# on_release: terminal branch (drop_lease=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_terminal_success_sets_done() -> None:
    # gate_state seeded non-None: a call that was gated earlier in its
    # life, then resumed and ran to completion, must not leave a stale
    # gate payload behind on the terminal row.
    # batch_task_ids empty (not part of a tool_wait batch): no sibling
    # check, on_release returns None - a lone claimed call outside the
    # claim-based dispatch seam.
    task = _make_task("t2", gate_state={"resume_event_payload": {"decision": "approved"}})
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    result = await adapter.on_release(
        conn=None, entity_id="t2",
        outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    assert result is None
    assert len(storage.updated) == 1
    updated = storage.updated[0]
    assert updated.state == ToolCallTaskState.DONE
    assert updated.finished_at is not None
    assert updated.last_error is None
    assert updated.gate_state is None


@pytest.mark.asyncio
async def test_on_release_terminal_failure_sets_failed_with_error() -> None:
    """A poisoned task (fail-count cap exceeded upstream): the caller has
    already written the failed TOOL_RESULT record and releases with
    success=False, drop_lease=True to signal "stop retrying, this is
    terminal" rather than requeuing it forever."""
    task = _make_task("t3")
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="t3",
        outcome=ReleaseOutcome(
            success=False, drop_lease=True, last_error="tool raised OSError",
        ),
    )
    assert len(storage.updated) == 1
    updated = storage.updated[0]
    assert updated.state == ToolCallTaskState.FAILED
    assert updated.finished_at is not None
    assert updated.last_error == "tool raised OSError"


# ---------------------------------------------------------------------------
# on_release: retryable branch (drop_lease=False, not gated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_retryable_failure_resets_to_queued() -> None:
    """A transient failure (reclaim, worker crash) - not terminal, not
    gated - resets to QUEUED so the next claim (this worker or another)
    picks it up again. The engine's own lease.attempt_count is the
    authoritative retry counter; this row just stops reading RUNNING once
    nobody is actually running it."""
    task = _make_task(
        "t4", state=ToolCallTaskState.RUNNING,
        gate_state={"resume_event_payload": {"decision": "approved"}},
    )
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="t4",
        outcome=ReleaseOutcome(success=False, last_error="reclaim"),
    )
    assert len(storage.updated) == 1
    updated = storage.updated[0]
    assert updated.state == ToolCallTaskState.QUEUED
    assert updated.started_at is None
    assert updated.gate_state is None


# ---------------------------------------------------------------------------
# on_release: missing storage / missing row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_raises_without_storage() -> None:
    adapter = ToolCallClaimAdapter(task_storage=None)
    with pytest.raises(RuntimeError, match="task_storage is None"):
        await adapter.on_release(
            conn=None, entity_id="ghost",
            outcome=ReleaseOutcome(success=True, drop_lease=True),
        )


@pytest.mark.asyncio
async def test_on_release_no_op_when_row_missing() -> None:
    task = _make_task("t5")
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="does-not-exist",
        outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    assert storage.updated == []


# ---------------------------------------------------------------------------
# on_release: last-sibling wake signal (01a0518b review, mixed-park wake
# seam) - PostReleaseWake is returned, never acted on directly (the
# adapter must not call durably_mark_session_resumable itself - see that
# class's own docstring for the transactional-visibility hazard this
# split avoids).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_returns_none_when_sibling_still_outstanding() -> None:
    batch = ["b:tool:0:1", "b:tool:0:2"]
    releasing = _make_task(
        "b:tool:0:1", state=ToolCallTaskState.RUNNING, batch_task_ids=batch,
    )
    sibling_still_queued = _make_task(
        "b:tool:0:2", state=ToolCallTaskState.QUEUED, batch_task_ids=batch,
    )
    storage = FakeMultiStorage([releasing, sibling_still_queued])
    adapter = ToolCallClaimAdapter(task_storage=storage)

    result = await adapter.on_release(
        conn=None, entity_id="b:tool:0:1",
        outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    assert result is None
    # The releasing task's own row IS updated to DONE regardless - only
    # the WAKE is deferred, not this task's own terminal state.
    updated = await storage.get("b:tool:0:1")
    assert updated.state == ToolCallTaskState.DONE


@pytest.mark.asyncio
async def test_on_release_returns_wake_signal_when_last_sibling() -> None:
    batch = ["b:tool:0:1", "b:tool:0:2"]
    releasing = _make_task(
        "b:tool:0:1", state=ToolCallTaskState.RUNNING, batch_task_ids=batch,
    )
    sibling_already_done = _make_task(
        "b:tool:0:2", state=ToolCallTaskState.DONE, batch_task_ids=batch,
    )
    storage = FakeMultiStorage([releasing, sibling_already_done])
    adapter = ToolCallClaimAdapter(task_storage=storage)

    result = await adapter.on_release(
        conn=None, entity_id="b:tool:0:1",
        outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    # event_key's node segment ("b") comes from the releasing task's own
    # scoped id ("b:tool:0:1") - see tool_wait_event_key's own docstring.
    assert result == PostReleaseWake(
        session_id="sess-1",
        event_key="tool_wait:sess-1:0:b",
        payload={"tool_wait_ready": True},
    )


@pytest.mark.asyncio
async def test_on_release_wake_signal_treats_failed_as_terminal() -> None:
    """A sibling that FAILED (not just DONE) still counts as terminal for
    the purposes of "is the batch finished" - the batch is done either
    way, the resume coordinator is what decides how to represent a
    failed sibling's result."""
    batch = ["b:tool:0:1", "b:tool:0:2"]
    releasing = _make_task(
        "b:tool:0:1", state=ToolCallTaskState.RUNNING, batch_task_ids=batch,
    )
    sibling_failed = _make_task(
        "b:tool:0:2", state=ToolCallTaskState.FAILED, batch_task_ids=batch,
    )
    storage = FakeMultiStorage([releasing, sibling_failed])
    adapter = ToolCallClaimAdapter(task_storage=storage)

    result = await adapter.on_release(
        conn=None, entity_id="b:tool:0:1",
        outcome=ReleaseOutcome(success=False, drop_lease=True, last_error="boom"),
    )
    assert result is not None
    assert result.session_id == "sess-1"


@pytest.mark.asyncio
async def test_on_release_reads_siblings_via_get_not_find() -> None:
    """The whole point of returning a signal instead of writing directly
    is a conn-scoped read - prove the sibling check goes through
    storage.get (which accepts conn) for every id in batch_task_ids,
    never a bulk find()."""
    batch = ["b:tool:0:1", "b:tool:0:2", "b:tool:0:3"]
    releasing = _make_task(
        "b:tool:0:1", state=ToolCallTaskState.RUNNING, batch_task_ids=batch,
    )
    sibling_a = _make_task(
        "b:tool:0:2", state=ToolCallTaskState.DONE, batch_task_ids=batch,
    )
    sibling_b = _make_task(
        "b:tool:0:3", state=ToolCallTaskState.DONE, batch_task_ids=batch,
    )
    storage = FakeMultiStorage([releasing, sibling_a, sibling_b])
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="b:tool:0:1",
        outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    # on_release's own top-level fetch reads "b:tool:0:1" once (the entity
    # being released); the sibling-check loop reads every OTHER id
    # individually but reuses the just-updated in-memory copy for its OWN
    # id rather than re-fetching it a second time.
    assert sorted(storage.get_calls) == [
        "b:tool:0:1", "b:tool:0:2", "b:tool:0:3",
    ]
    assert storage.get_calls.count("b:tool:0:1") == 1
