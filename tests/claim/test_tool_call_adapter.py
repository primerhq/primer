"""Unit tests for ToolCallClaimAdapter (Phase 3 stage 7a, 01a0518b).

Mirrors tests/claim/test_session_adapter.py's shape (eligibility_sql,
entity_indexes, on_release scenarios via a FakeStorage double) - the
established pattern for this test class.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.int.claim import ClaimKind, ParkRequest, ReleaseOutcome
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_task(
    task_id: str,
    *,
    state: ToolCallTaskState = ToolCallTaskState.RUNNING,
    gate_state: dict | None = None,
) -> ToolCallTask:
    return ToolCallTask(
        id=task_id,
        session_id="sess-1",
        turn_no=0,
        tool_name="workspace__write",
        state=state,
        gate_state=gate_state,
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
    task = _make_task("t2", gate_state={"resume_event_payload": {"decision": "approved"}})
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="t2",
        outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
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
