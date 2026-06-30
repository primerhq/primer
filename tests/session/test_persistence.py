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
    """StreamStart, ReasoningDelta etc. are silently dropped (return None)."""
    from primer.model.chat import ReasoningDelta, StreamStart
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    assert translate_stream_event(StreamStart(model="x", request_id=None), state) is None
    assert (
        translate_stream_event(ReasoningDelta(text="think", index=0), state) is None
    )


def test_translate_usage_accumulates_in_state() -> None:
    """Usage events are accumulated in _CoalesceState (not dropped to a record).

    The agent-only path keys its accumulated Usage under the ``None`` bucket.
    """
    from primer.model.chat import Usage
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    result = translate_stream_event(
        Usage(input_tokens=100, output_tokens=50, cumulative=True), state
    )
    assert result is None
    assert state.last_usage_by.get(None) is not None
    assert state.last_usage_by[None].input_tokens == 100
    assert state.last_usage_by[None].output_tokens == 50


def test_translate_done_includes_usage_envelope() -> None:
    """DONE record payload carries a usage dict when a Usage event preceded it."""
    from primer.model.chat import Done, Usage
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    translate_stream_event(
        Usage(input_tokens=200, output_tokens=75, cumulative=True), state
    )
    result = translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state)

    assert isinstance(result, SessionMessageRecord)
    assert result.kind == SessionMessageKind.DONE
    assert result.payload["stop_reason"] == "stop"
    assert "usage" in result.payload
    assert result.payload["usage"]["input_tokens"] == 200
    assert result.payload["usage"]["output_tokens"] == 75


def test_translate_done_no_usage_has_no_usage_key() -> None:
    """DONE record payload has no 'usage' key when no Usage event preceded it."""
    from primer.model.chat import Done
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    result = translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state)

    assert isinstance(result, SessionMessageRecord)
    assert result.kind == SessionMessageKind.DONE
    assert "usage" not in result.payload


def test_translate_done_usage_with_optional_fields() -> None:
    """DONE payload usage includes cached_input_tokens and reasoning_tokens when present."""
    from primer.model.chat import Done, Usage
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    translate_stream_event(
        Usage(
            input_tokens=300,
            output_tokens=100,
            cached_input_tokens=50,
            reasoning_tokens=20,
            cumulative=True,
        ),
        state,
    )
    result = translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state)

    assert isinstance(result, SessionMessageRecord)
    usage = result.payload["usage"]
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 100
    assert usage["cached_input_tokens"] == 50
    assert usage["reasoning_tokens"] == 20


# ---------------------------------------------------------------------------
# F1a: per-graph-node agent events flow into the session log, attributed by
# node_id (the wrapped _GraphNodeEvent un-drop + per-node coalescing).
# ---------------------------------------------------------------------------


def _wrap_node(node_id: str, inner):
    """Wrap an inner StreamEvent the way the graph executor does."""
    from primer.model.chat import ExtendedEvent, _GraphNodeEvent

    return ExtendedEvent(
        extended=_GraphNodeEvent(
            node_id=node_id,
            iteration=0,
            inner_type=inner.type,
            inner_payload=inner.model_dump(mode="json"),
        )
    )


def test_translate_wrapped_node_text_then_done_carries_node_id() -> None:
    """A wrapped node TextDelta+Done yields ASSISTANT_TOKEN(+DONE) with node_id set."""
    from primer.model.chat import Done, TextDelta
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    r1 = translate_stream_event(_wrap_node("n1", TextDelta(text="hi ", index=0)), state)
    r2 = translate_stream_event(_wrap_node("n1", TextDelta(text="there", index=0)), state)
    r3 = translate_stream_event(
        _wrap_node("n1", Done(stop_reason="stop", raw_reason="stop")), state
    )

    assert r1 is None
    assert r2 is None
    assert isinstance(r3, list)
    assert len(r3) == 2
    assert r3[0].kind == SessionMessageKind.ASSISTANT_TOKEN
    assert r3[0].payload == {"text": "hi there"}
    assert r3[0].node_id == "n1"
    assert r3[1].kind == SessionMessageKind.DONE
    assert r3[1].node_id == "n1"


def test_wrapped_node_record_roundtrips_to_tap_event_with_node_id() -> None:
    """A node-attributed record maps through record_to_tap_event with node_id."""
    from primer.model.chat import TextDelta, ToolCallEnd
    from primer.session.persistence import _CoalesceState, translate_stream_event
    from primer.tap.event import record_to_tap_event

    state = _CoalesceState()
    translate_stream_event(_wrap_node("nX", TextDelta(text="x", index=0)), state)
    result = translate_stream_event(
        _wrap_node("nX", ToolCallEnd(id="tc", arguments={}, index=0)), state
    )
    assert isinstance(result, list)
    token = result[0]
    event = record_to_tap_event(
        token,
        workspace_id="ws",
        session_id="s",
        agent_id="a",
        graph_id="g",
        cursor="c",
    )
    assert event.node_id == "nX"


def test_translate_wrapped_node_tool_call_carries_node_id() -> None:
    """A wrapped node ToolCallEnd yields TOOL_CALL with node_id."""
    from primer.model.chat import ToolCallEnd
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec = translate_stream_event(
        _wrap_node("nT", ToolCallEnd(id="tc1", arguments={"x": 1}, index=0)), state
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.TOOL_CALL
    assert rec.payload["id"] == "tc1"
    assert rec.node_id == "nT"


def test_translate_wrapped_node_tool_result_nesting() -> None:
    """The nesting case: _GraphNodeEvent wrapping ExtendedEvent(_ExecutorToolResult)
    reconstructs + recurses into a TOOL_RESULT record with node_id."""
    from primer.model.chat import ExtendedEvent, _ExecutorToolResult
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    inner = ExtendedEvent(
        extended=_ExecutorToolResult(call_id="c1", output="out", error=False)
    )
    rec = translate_stream_event(_wrap_node("nR", inner), state)
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.TOOL_RESULT
    assert rec.payload["call_id"] == "c1"
    assert rec.payload["output"] == "out"
    assert rec.node_id == "nR"


def test_concurrent_nodes_text_does_not_mix() -> None:
    """Interleaved TextDeltas from two nodes flush to disjoint, uncrossed text."""
    from primer.model.chat import Done, TextDelta
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    # Interleave node "a" and node "b" deltas.
    translate_stream_event(_wrap_node("a", TextDelta(text="alpha-", index=0)), state)
    translate_stream_event(_wrap_node("b", TextDelta(text="beta-", index=0)), state)
    translate_stream_event(_wrap_node("a", TextDelta(text="A", index=0)), state)
    translate_stream_event(_wrap_node("b", TextDelta(text="B", index=0)), state)

    ra = translate_stream_event(
        _wrap_node("a", Done(stop_reason="stop", raw_reason="stop")), state
    )
    rb = translate_stream_event(
        _wrap_node("b", Done(stop_reason="stop", raw_reason="stop")), state
    )

    assert isinstance(ra, list) and isinstance(rb, list)
    a_token = ra[0]
    b_token = rb[0]
    assert a_token.payload["text"] == "alpha-A"
    assert a_token.node_id == "a"
    assert b_token.payload["text"] == "beta-B"
    assert b_token.node_id == "b"
    # No cross-contamination either way.
    assert "beta" not in a_token.payload["text"]
    assert "alpha" not in b_token.payload["text"]


def test_concurrent_nodes_usage_does_not_mix() -> None:
    """Each node's Done carries ITS node's usage, not a sibling's."""
    from primer.model.chat import Done, Usage
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    translate_stream_event(
        _wrap_node("a", Usage(input_tokens=10, output_tokens=1, cumulative=True)), state
    )
    translate_stream_event(
        _wrap_node("b", Usage(input_tokens=99, output_tokens=9, cumulative=True)), state
    )
    ra = translate_stream_event(
        _wrap_node("a", Done(stop_reason="stop", raw_reason="stop")), state
    )
    rb = translate_stream_event(
        _wrap_node("b", Done(stop_reason="stop", raw_reason="stop")), state
    )
    assert isinstance(ra, SessionMessageRecord)
    assert isinstance(rb, SessionMessageRecord)
    assert ra.payload["usage"]["input_tokens"] == 10
    assert rb.payload["usage"]["input_tokens"] == 99


def test_agent_only_path_unchanged_node_id_none() -> None:
    """The default (node_id=None) path is identical to today: node_id None."""
    from primer.model.chat import Done, TextDelta
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    assert translate_stream_event(TextDelta(text="hello ", index=0), state) is None
    assert translate_stream_event(TextDelta(text="world", index=0), state) is None
    result = translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state)
    assert isinstance(result, list)
    assert result[0].kind == SessionMessageKind.ASSISTANT_TOKEN
    assert result[0].payload == {"text": "hello world"}
    assert result[0].node_id is None
    assert result[1].kind == SessionMessageKind.DONE
    assert result[1].node_id is None


def test_wrapped_node_unreconstructable_inner_is_dropped() -> None:
    """A _GraphNodeEvent whose inner_payload isn't a valid StreamEvent drops (None)."""
    from primer.model.chat import ExtendedEvent, _GraphNodeEvent
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    bogus = ExtendedEvent(
        extended=_GraphNodeEvent(
            node_id="nB",
            iteration=0,
            inner_type="not_a_real_type",
            inner_payload={"type": "not_a_real_type"},
        )
    )
    assert translate_stream_event(bogus, state) is None


# ---------------------------------------------------------------------------
# Graph lifecycle records now carry a first-class node_id
# ---------------------------------------------------------------------------


def test_graph_transition_record_carries_node_id() -> None:
    from primer.graph.base import _GraphTransitionEvent
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec = translate_stream_event(
        _GraphTransitionEvent(
            node_id="gn1", node_kind="agent", phase="enter", status=None
        ),
        state,
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.GRAPH_TRANSITION
    assert rec.node_id == "gn1"
    assert rec.payload["node_id"] == "gn1"


def test_graph_error_record_carries_node_id() -> None:
    from primer.graph.base import _GraphErrorEvent
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec = translate_stream_event(
        _GraphErrorEvent(code="x", message="boom", node_id="gn2", path=None), state
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.node_id == "gn2"


def test_graph_end_output_record_carries_node_id() -> None:
    from primer.graph.base import _GraphEndOutputEvent
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec = translate_stream_event(
        _GraphEndOutputEvent(text="final", parsed=None, end_node_id="end1"), state
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.ASSISTANT_TOKEN
    assert rec.node_id == "end1"
    assert rec.payload["end_node_id"] == "end1"
