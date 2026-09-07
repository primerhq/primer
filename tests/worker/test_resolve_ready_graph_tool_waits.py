"""``resolve_ready_graph_tool_waits`` (Phase 3 stage 7a, 01a0518b boundary
d) - the shared per-node readiness check that closes a real routing gap:
``resume_graph_engine`` (the classic human-gate graph resume) previously
never checked tool_wait readiness at all, so a co-pending batch that went
terminal WHILE a human gate was still unanswered would sit silently
stranded until some unrelated later reply happened to drain it. Both
``resume_graph_engine`` and the pure-path ``resume_graph_tool_wait`` now
call this same function on every drain cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.worker.tool_wait_resume_coordinator import resolve_ready_graph_tool_waits


class _FakeTaskStorage:
    def __init__(self, tasks: dict[str, ToolCallTask]) -> None:
        self._tasks = tasks
        self.get_calls: list[str] = []

    async def get(self, task_id: str):
        self.get_calls.append(task_id)
        return self._tasks.get(task_id)


def _task(
    task_id: str, state: ToolCallTaskState, *,
    output: str | None = None, error: bool = False, last_error: str | None = None,
) -> ToolCallTask:
    return ToolCallTask(
        id=task_id, session_id="s1", turn_no=0, tool_name="t",
        state=state, record_seq=1, created_at=datetime.now(timezone.utc),
        result_state=(
            {"id": task_id, "output": output, "error": error}
            if output is not None else None
        ),
        last_error=last_error,
    )


def _pending_tool_wait(node_id: str, outstanding: list[str], notifying: list[str]) -> dict:
    return {
        "node_id": node_id,
        "outstanding_task_ids": outstanding,
        "notifying_results": [(nid, {"id": nid, "output": "inline", "error": False}) for nid in notifying],
    }


@pytest.mark.asyncio
async def test_ready_node_resolves_result_parts_and_tasks() -> None:
    storage = _FakeTaskStorage({
        "A:tool:0:1": _task("A:tool:0:1", ToolCallTaskState.DONE, output="result A"),
    })
    pending = [_pending_tool_wait("A", ["A:tool:0:1"], [])]

    resolved_tool_wait, resolved_tasks = await resolve_ready_graph_tool_waits(
        storage, pending,
    )

    assert set(resolved_tool_wait) == {"A"}
    assert resolved_tool_wait["A"][0].id == "A:tool:0:1"
    assert resolved_tool_wait["A"][0].output == "result A"
    assert [t.id for t in resolved_tasks["A"]] == ["A:tool:0:1"]


@pytest.mark.asyncio
async def test_not_ready_node_is_absent_from_both_dicts() -> None:
    """QUEUED (not yet terminal) - the node stays OUT of both returned
    dicts entirely, not present-with-empty-results; the caller's own
    _pending_tool_waits then correctly keeps it pending for a later
    drain (partial wake)."""
    storage = _FakeTaskStorage({
        "B:tool:0:1": _task("B:tool:0:1", ToolCallTaskState.QUEUED),
    })
    pending = [_pending_tool_wait("B", ["B:tool:0:1"], [])]

    resolved_tool_wait, resolved_tasks = await resolve_ready_graph_tool_waits(
        storage, pending,
    )

    assert resolved_tool_wait == {}
    assert resolved_tasks == {}


@pytest.mark.asyncio
async def test_missing_task_row_treated_as_not_ready() -> None:
    storage = _FakeTaskStorage({})
    pending = [_pending_tool_wait("A", ["A:tool:0:1"], [])]

    resolved_tool_wait, resolved_tasks = await resolve_ready_graph_tool_waits(
        storage, pending,
    )

    assert resolved_tool_wait == {}
    assert resolved_tasks == {}


@pytest.mark.asyncio
async def test_partial_wake_only_ready_node_resolves() -> None:
    """The exact shape the routing gap fix exists for: node A's batch is
    fully terminal while node B's is still mid-flight in the SAME
    drain cycle - only A comes back, B is left for later."""
    storage = _FakeTaskStorage({
        "A:tool:0:1": _task("A:tool:0:1", ToolCallTaskState.DONE, output="result A"),
        "B:tool:0:1": _task("B:tool:0:1", ToolCallTaskState.QUEUED),
    })
    pending = [
        _pending_tool_wait("A", ["A:tool:0:1"], []),
        _pending_tool_wait("B", ["B:tool:0:1"], []),
    ]

    resolved_tool_wait, resolved_tasks = await resolve_ready_graph_tool_waits(
        storage, pending,
    )

    assert set(resolved_tool_wait) == {"A"}
    assert set(resolved_tasks) == {"A"}


@pytest.mark.asyncio
async def test_failed_task_with_no_result_state_synthesizes_error_part() -> None:
    """A poisoned claim that never ran still owes the LLM a tool_result
    for its tool_use - synthesised from last_error, id preserved so the
    pairing with its tool_use is not dropped."""
    storage = _FakeTaskStorage({
        "A:tool:0:1": _task(
            "A:tool:0:1", ToolCallTaskState.FAILED, last_error="boom",
        ),
    })
    pending = [_pending_tool_wait("A", ["A:tool:0:1"], [])]

    resolved_tool_wait, _resolved_tasks = await resolve_ready_graph_tool_waits(
        storage, pending,
    )

    part = resolved_tool_wait["A"][0]
    assert part.id == "A:tool:0:1"
    assert part.output == "boom"
    assert part.error is True


@pytest.mark.asyncio
async def test_notifying_results_count_toward_readiness_too() -> None:
    """A notifying call is answered inline and stored terminal from the
    start - its own id is part of "every id in the batch" readiness, not
    just the claimable ones."""
    storage = _FakeTaskStorage({
        "A:tool:0:1": _task("A:tool:0:1", ToolCallTaskState.DONE, output="result A"),
        "A:tool:0:2": _task("A:tool:0:2", ToolCallTaskState.DONE, output="notified"),
    })
    pending = [_pending_tool_wait("A", ["A:tool:0:1"], ["A:tool:0:2"])]

    resolved_tool_wait, resolved_tasks = await resolve_ready_graph_tool_waits(
        storage, pending,
    )

    assert set(resolved_tool_wait) == {"A"}
    assert {p.id for p in resolved_tool_wait["A"]} == {"A:tool:0:1", "A:tool:0:2"}
    assert {t.id for t in resolved_tasks["A"]} == {"A:tool:0:1", "A:tool:0:2"}
