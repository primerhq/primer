"""Tests for primer.claim.tool_call_lookup (Phase 3 stage 7a, 01a0518b).

Pins the "fail loudly on mismatch, no silent scan-fallback" ruling: a
task's record_seq pointer must resolve to a TOOL_CALL record whose own
id matches the task's id, or ToolCallRecordMismatch is raised.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.claim.tool_call_lookup import ToolCallRecordMismatch, read_tool_call_record
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class _FakeWorkspaceIO:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: bytes) -> None:
        self._files[path] = content

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        return self._files[path]


def _msg_path(session_id: str) -> str:
    return f".state/sessions/{session_id}/messages.jsonl"


def _line(seq: int, kind: str, payload: dict | None = None) -> bytes:
    rec = {
        "seq": seq, "kind": kind, "payload": payload or {},
        "created_at": _NOW.isoformat(),
    }
    return (json.dumps(rec) + "\n").encode()


def _make_task(*, id: str = "w:tool:1:1", record_seq: int = 2) -> ToolCallTask:
    return ToolCallTask(
        id=id,
        session_id="sess-1",
        turn_no=1,
        tool_name="workspace__write",
        state=ToolCallTaskState.QUEUED,
        record_seq=record_seq,
        created_at=_NOW,
    )


@pytest.mark.asyncio
async def test_reads_the_matching_tool_call_record() -> None:
    io = _FakeWorkspaceIO()
    io.write(
        _msg_path("sess-1"),
        _line(1, "user_input")
        + _line(2, "tool_call", {"id": "w:tool:1:1", "name": "workspace__write"})
        + _line(3, "done"),
    )
    task = _make_task()

    record = await read_tool_call_record(io, task)

    assert record.seq == 2
    assert record.payload["name"] == "workspace__write"


@pytest.mark.asyncio
async def test_raises_when_log_missing() -> None:
    io = _FakeWorkspaceIO()
    task = _make_task()
    with pytest.raises(ToolCallRecordMismatch, match="no record"):
        await read_tool_call_record(io, task)


@pytest.mark.asyncio
async def test_raises_when_no_record_at_seq() -> None:
    io = _FakeWorkspaceIO()
    io.write(_msg_path("sess-1"), _line(1, "user_input"))
    task = _make_task(record_seq=2)
    with pytest.raises(ToolCallRecordMismatch, match="no record"):
        await read_tool_call_record(io, task)


@pytest.mark.asyncio
async def test_raises_when_record_is_not_tool_call_kind() -> None:
    """The ordering invariant broke: record_seq points at something else."""
    io = _FakeWorkspaceIO()
    io.write(_msg_path("sess-1"), _line(2, "done", {}))
    task = _make_task(record_seq=2)
    with pytest.raises(ToolCallRecordMismatch, match="expected TOOL_CALL"):
        await read_tool_call_record(io, task)


@pytest.mark.asyncio
async def test_raises_on_id_mismatch_no_silent_fallback() -> None:
    """A different call's TOOL_CALL record happens to sit at this seq -
    must fail loudly, never silently accept the wrong record."""
    io = _FakeWorkspaceIO()
    io.write(
        _msg_path("sess-1"),
        _line(2, "tool_call", {"id": "some-other-task", "name": "x"}),
    )
    task = _make_task(id="w:tool:1:1", record_seq=2)
    with pytest.raises(ToolCallRecordMismatch, match="expected 'w:tool:1:1'"):
        await read_tool_call_record(io, task)
