"""Tests for WorkspaceMessageWriter — buffered jsonl appender.

Buffer policy:
  - flush when accumulated bytes >= 16 KB
  - flush when first buffered record is >= 100 ms old
  - flush on explicit flush() / aclose()
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.persistence import WorkspaceMessageWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fake workspace_io
# ---------------------------------------------------------------------------


class FakeWorkspaceIO:
    """In-memory workspace I/O shim used by tests.

    Stores appended bytes per (session_id, filename) key so tests can
    inspect what was persisted without touching the filesystem.
    """

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = defaultdict(bytes)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        """Append a jsonl line (including trailing newline) to the session store."""
        self._data[(session_id, "messages.jsonl")] += line

    def read_lines(self, session_id: str, filename: str) -> list[str]:
        """Return non-empty decoded lines (strips trailing newlines)."""
        raw = self._data.get((session_id, filename), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]


@pytest.fixture
def fake_workspace_io() -> FakeWorkspaceIO:
    return FakeWorkspaceIO()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_append_persists_record_returning_seq(
    fake_workspace_io: FakeWorkspaceIO,
) -> None:
    w = WorkspaceMessageWriter(workspace_io=fake_workspace_io, session_id="s1")
    seq = await w.append(
        SessionMessageRecord(
            seq=1, kind=SessionMessageKind.DONE, payload={}, created_at=now()
        )
    )
    assert seq == 1  # writer assigns seq=1 (first record)
    await w.flush()
    lines = fake_workspace_io.read_lines("s1", "messages.jsonl")
    assert len(lines) == 1
    assert json.loads(lines[0])["seq"] == 1


async def test_flush_writes_all_buffered_records(
    fake_workspace_io: FakeWorkspaceIO,
) -> None:
    w = WorkspaceMessageWriter(workspace_io=fake_workspace_io, session_id="s1")
    await w.append(
        SessionMessageRecord(seq=1, kind=SessionMessageKind.DONE, payload={}, created_at=now())
    )
    await w.append(
        SessionMessageRecord(seq=1, kind=SessionMessageKind.DONE, payload={}, created_at=now())
    )
    # No lines yet (buffer not yet flushed by policy)
    lines_before = fake_workspace_io.read_lines("s1", "messages.jsonl")
    assert len(lines_before) == 0
    await w.flush()
    lines = fake_workspace_io.read_lines("s1", "messages.jsonl")
    assert len(lines) == 2


async def test_buffer_flushes_at_16kb(fake_workspace_io: FakeWorkspaceIO) -> None:
    w = WorkspaceMessageWriter(workspace_io=fake_workspace_io, session_id="s1")
    # Fill > 16 KB without explicit flush; each record ~130 bytes so
    # 200 records ~ 26 KB → auto-flush must have fired.
    for _ in range(200):
        await w.append(
            SessionMessageRecord(
                seq=1,
                kind=SessionMessageKind.ASSISTANT_TOKEN,
                payload={"delta": "x" * 100},
                created_at=now(),
            )
        )
    lines = fake_workspace_io.read_lines("s1", "messages.jsonl")
    assert len(lines) > 0


async def test_buffer_flushes_after_100ms(fake_workspace_io: FakeWorkspaceIO) -> None:
    w = WorkspaceMessageWriter(workspace_io=fake_workspace_io, session_id="s1")
    await w.append(
        SessionMessageRecord(
            seq=1, kind=SessionMessageKind.DONE, payload={}, created_at=now()
        )
    )
    await asyncio.sleep(0.15)
    # Second append should detect that the first record is > 100 ms old and flush.
    await w.append(
        SessionMessageRecord(
            seq=1, kind=SessionMessageKind.DONE, payload={}, created_at=now()
        )
    )
    lines = fake_workspace_io.read_lines("s1", "messages.jsonl")
    assert len(lines) >= 1


async def test_seq_is_monotonic(fake_workspace_io: FakeWorkspaceIO) -> None:
    w = WorkspaceMessageWriter(workspace_io=fake_workspace_io, session_id="s1")
    seqs = [
        await w.append(
            SessionMessageRecord(
                seq=1,
                kind=SessionMessageKind.ASSISTANT_TOKEN,
                payload={},
                created_at=now(),
            )
        )
        for _ in range(5)
    ]
    assert seqs == [1, 2, 3, 4, 5]


async def test_aclose_flushes_remaining_buffer(
    fake_workspace_io: FakeWorkspaceIO,
) -> None:
    w = WorkspaceMessageWriter(workspace_io=fake_workspace_io, session_id="s1")
    await w.append(
        SessionMessageRecord(seq=1, kind=SessionMessageKind.DONE, payload={}, created_at=now())
    )
    await w.aclose()
    lines = fake_workspace_io.read_lines("s1", "messages.jsonl")
    assert len(lines) == 1


async def test_seq_written_into_persisted_record(
    fake_workspace_io: FakeWorkspaceIO,
) -> None:
    """The writer-assigned seq appears in the persisted jsonl, not the caller's seq."""
    w = WorkspaceMessageWriter(workspace_io=fake_workspace_io, session_id="s1")
    # Pass seq=99 as placeholder; writer should override with its counter.
    seq = await w.append(
        SessionMessageRecord(
            seq=99, kind=SessionMessageKind.DONE, payload={}, created_at=now()
        )
    )
    assert seq == 1  # always 1 for the first record
    await w.flush()
    lines = fake_workspace_io.read_lines("s1", "messages.jsonl")
    assert json.loads(lines[0])["seq"] == 1


# ---------------------------------------------------------------------------
# translate_stream_event tests
# ---------------------------------------------------------------------------


def test_translate_text_delta_coalesces() -> None:
    """Multiple TextDeltas in a row coalesce; Done flushes them as one assistant_token."""
    from primer.model.chat import Done, TextDelta
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec1 = translate_stream_event(TextDelta(text="hello ", index=0), state)
    rec2 = translate_stream_event(TextDelta(text="world", index=0), state)
    rec3 = translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state)

    assert rec1 is None
    assert rec2 is None
    # Done flushes the coalesced text then emits a done record
    assert isinstance(rec3, list)
    assert len(rec3) == 2
    assert rec3[0].kind == SessionMessageKind.ASSISTANT_TOKEN
    assert rec3[0].payload == {"text": "hello world"}
    assert rec3[1].kind == SessionMessageKind.DONE


def test_translate_done_no_text_emits_only_done() -> None:
    """Done with no buffered text emits a single DONE record (not a list)."""
    from primer.model.chat import Done
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    result = translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state)
    # No coalesced text → single record, not a list
    assert isinstance(result, SessionMessageRecord)
    assert result.kind == SessionMessageKind.DONE
    assert result.payload.get("stop_reason") == "stop"


def test_translate_tool_call_end() -> None:
    """ToolCallEnd emits a TOOL_CALL record."""
    from primer.model.chat import ToolCallEnd
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec = translate_stream_event(
        ToolCallEnd(id="tc1", arguments={"x": 1}, index=0), state
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.TOOL_CALL
    assert rec.payload.get("id") == "tc1"


def test_translate_tool_call_end_flushes_text_buffer() -> None:
    """ToolCallEnd flushes any coalesced text first, then emits TOOL_CALL."""
    from primer.model.chat import TextDelta, ToolCallEnd
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    translate_stream_event(TextDelta(text="thinking", index=0), state)
    result = translate_stream_event(
        ToolCallEnd(id="tc2", arguments={}, index=1), state
    )
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0].kind == SessionMessageKind.ASSISTANT_TOKEN
    assert result[0].payload["text"] == "thinking"
    assert result[1].kind == SessionMessageKind.TOOL_CALL


def test_translate_executor_tool_result() -> None:
    """ExtendedEvent wrapping _ExecutorToolResult emits a TOOL_RESULT record."""
    from primer.model.chat import ExtendedEvent, _ExecutorToolResult
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    event = ExtendedEvent(
        extended=_ExecutorToolResult(call_id="tc1", output="result text", error=False)
    )
    rec = translate_stream_event(event, state)
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.TOOL_RESULT
    assert rec.payload.get("call_id") == "tc1"


def test_translate_error() -> None:
    """Error emits an ERROR record."""
    from primer.model.chat import Error
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec = translate_stream_event(Error(message="boom", code="x", fatal=True), state)
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.payload.get("message") == "boom"


def test_translate_dropped_events_return_none() -> None:
    """StreamStart, Usage, ReasoningDelta etc. are silently dropped (return None)."""
    from primer.model.chat import ReasoningDelta, StreamStart, Usage
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    assert translate_stream_event(StreamStart(model="x", request_id=None), state) is None
    assert (
        translate_stream_event(
            Usage(input_tokens=10, output_tokens=5, cumulative=True), state
        )
        is None
    )
    assert (
        translate_stream_event(ReasoningDelta(text="think", index=0), state) is None
    )
