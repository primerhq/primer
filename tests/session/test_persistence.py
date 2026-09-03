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
    assert rec3[0].payload == {"text": "hello world", "part_id": "x:text:0"}
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


def test_translate_tool_call_carries_name_from_start() -> None:
    """ToolCallStart records the tool name (which ToolCallEnd lacks) so the
    paired TOOL_CALL record persists it, id-keyed, instead of leaving the UI
    to fall back to the generic "tool" label."""
    from primer.model.chat import ToolCallEnd, ToolCallStart
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    # Start carries the name but produces no record of its own.
    assert (
        translate_stream_event(
            ToolCallStart(id="tc9", name="fs__write", index=0), state
        )
        is None
    )
    rec = translate_stream_event(
        ToolCallEnd(id="tc9", arguments={"path": "x"}, index=0), state
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.TOOL_CALL
    # 01a0518f: the durable id is the SCOPED id minted at ToolCallStart,
    # not the raw provider id verbatim - it restarts at the same value
    # every llm.stream() call, so bare "tc9" can legitimately collide
    # across tool-rounds and concurrent fan-out siblings.
    assert rec.payload.get("id") == "x:tool:0:1"
    assert rec.payload.get("name") == "fs__write"
    assert rec.payload.get("arguments") == {"path": "x"}
    # Consumed on End — the map doesn't accumulate dead keys.
    assert (None, "tc9") not in state.tool_names
    # The scoped-id mapping itself is NOT consumed on End - the later
    # _ExecutorToolResult event for this same call still needs it.
    assert state.scoped_call_ids[(None, "tc9")] == "x:tool:0:1"


def test_translate_tool_call_end_without_start_has_null_name() -> None:
    """A ToolCallEnd with no preceding ToolCallStart (name unknown) still
    emits a TOOL_CALL record; name is None and the UI falls back to "tool"."""
    from primer.model.chat import ToolCallEnd
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec = translate_stream_event(
        ToolCallEnd(id="tc-orphan", arguments={}, index=0), state
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.TOOL_CALL
    assert rec.payload.get("name") is None


class _FakeDeltaSink:
    def __init__(self):
        self.deltas: list[tuple[str, str, str]] = []
        self.closed: list[str] = []

    def on_delta(self, pid, kind, delta):
        self.deltas.append((pid, kind, delta))

    def close(self, pid):
        self.closed.append(pid)


def test_tool_call_round_trip_shares_one_scoped_id_across_call_delta_and_result() -> None:
    """01a0518f: ToolCallStart mints the scoped id; ToolCallDelta's live
    delta_sink calls, the durable TOOL_CALL record, and the LATER
    TOOL_RESULT record must all carry that SAME scoped id - not the raw
    provider id verbatim - so live-argument reconciliation and the
    TOOL_CALL/TOOL_RESULT pairing both still work with the new id shape."""
    from primer.model.chat import (
        ExtendedEvent,
        ToolCallDelta,
        ToolCallEnd,
        ToolCallStart,
        _ExecutorToolResult,
    )
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    sink = _FakeDeltaSink()

    assert translate_stream_event(
        ToolCallStart(id="call_0", name="fs__write", index=0), state,
        delta_sink=sink,
    ) is None
    assert translate_stream_event(
        ToolCallDelta(id="call_0", arguments_delta='{"path"', index=0), state,
        delta_sink=sink,
    ) is None

    call_rec = translate_stream_event(
        ToolCallEnd(id="call_0", arguments={"path": "x"}, index=0), state,
        delta_sink=sink,
    )
    scoped_id = call_rec.payload["id"]
    assert scoped_id != "call_0"  # not the raw id verbatim

    result_rec = translate_stream_event(
        ExtendedEvent(extended=_ExecutorToolResult(
            call_id="call_0", output="done", error=False,
        )),
        state,
    )
    assert result_rec.payload["call_id"] == scoped_id, (
        "TOOL_CALL and TOOL_RESULT must carry the identical scoped id"
    )
    # Live delta_sink calls used the scoped id throughout, not the raw one.
    assert sink.deltas == [(scoped_id, "tool", '{"path"')]
    assert scoped_id in sink.closed
    # The mapping is popped once the result consumes it - not left dangling.
    assert (None, "call_0") not in state.scoped_call_ids


def test_two_tool_rounds_on_the_same_node_get_distinct_scoped_ids_for_the_same_raw_id() -> None:
    """01a0518f: THE bug this fix exists for. The LLM adapter's id
    counter restarts on every llm.stream() call (persistence.py's own
    module comment), so a SECOND tool-calling round on the SAME graph
    node can legitimately reuse "call_0" for an entirely different tool
    call. The two TOOL_CALL records must not collide."""
    from primer.model.chat import ToolCallEnd, ToolCallStart
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    node = "worker"

    translate_stream_event(
        ToolCallStart(id="call_0", name="fs__read", index=0), state,
        node_id=node,
    )
    round1 = translate_stream_event(
        ToolCallEnd(id="call_0", arguments={"path": "a"}, index=0), state,
        node_id=node,
    )

    translate_stream_event(
        ToolCallStart(id="call_0", name="fs__write", index=0), state,
        node_id=node,
    )
    round2 = translate_stream_event(
        ToolCallEnd(id="call_0", arguments={"path": "b"}, index=0), state,
        node_id=node,
    )

    assert round1.payload["id"] != round2.payload["id"], (
        f"two distinct tool-rounds sharing raw id 'call_0' collided: "
        f"{round1.payload['id']!r}"
    )


def test_concurrent_fanout_siblings_get_distinct_scoped_ids_for_the_same_raw_id() -> None:
    """01a0518f: two concurrent fan-out sibling nodes each restart their
    OWN LLM stream call's id counter independently, so they can also
    legitimately share a raw id within the SAME turn/round."""
    from primer.model.chat import ToolCallEnd, ToolCallStart
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()

    translate_stream_event(
        ToolCallStart(id="call_0", name="fs__read", index=0), state,
        node_id="worker[0]",
    )
    sibling0 = translate_stream_event(
        ToolCallEnd(id="call_0", arguments={}, index=0), state,
        node_id="worker[0]",
    )
    translate_stream_event(
        ToolCallStart(id="call_0", name="fs__read", index=0), state,
        node_id="worker[1]",
    )
    sibling1 = translate_stream_event(
        ToolCallEnd(id="call_0", arguments={}, index=0), state,
        node_id="worker[1]",
    )

    assert sibling0.payload["id"] != sibling1.payload["id"]


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


def test_translate_executor_tool_result_carries_metadata() -> None:
    """UX reconcile wave 5: the last of three drop points on this path
    (ToolResultPart -> _ExecutorToolResult -> here) - a workspace tool's
    own extra data (grep's match_count/file_count, ...) now survives
    into the persisted record instead of being silently dropped."""
    from primer.model.chat import ExtendedEvent, _ExecutorToolResult
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    event = ExtendedEvent(
        extended=_ExecutorToolResult(
            call_id="tc1", output="a.py\nb.py", error=False,
            metadata={"match_count": 3, "file_count": 2},
        )
    )
    rec = translate_stream_event(event, state)
    assert rec.payload.get("metadata") == {"match_count": 3, "file_count": 2}


def test_translate_executor_tool_result_metadata_defaults_to_none() -> None:
    """Additive and defensive: the overwhelming majority of tool
    results never set metadata, so this must not turn into a payload
    full of empty {} noise."""
    from primer.model.chat import ExtendedEvent, _ExecutorToolResult
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    event = ExtendedEvent(
        extended=_ExecutorToolResult(call_id="tc1", output="result text", error=False)
    )
    rec = translate_stream_event(event, state)
    assert rec.payload.get("metadata") is None


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
# 01a0690a: stash_graph_scoped_ids — graph-park scoped-id/seq stashing
# ---------------------------------------------------------------------------


def test_stash_graph_scoped_ids_none_checkpoint_is_noop() -> None:
    """An agent-bound park (no graph_checkpoint) has nothing to stash."""
    from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids

    state = _CoalesceState()
    assert stash_graph_scoped_ids(None, state) == {}


def test_stash_graph_scoped_ids_sets_matching_entries() -> None:
    """Each pending entry's scoped_tool_call_id is set from coalesce_state,
    keyed by (node_id, tool_call_id); the per-node seq snapshot is returned
    for ParkedState.node_tool_call_seq."""
    from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids

    state = _CoalesceState()
    state.scoped_call_ids[("worker[0]", "tc-1")] = "worker[0]:tool:5:1"
    state.scoped_call_ids[("asker", "tc-2")] = "asker:tool:5:1"
    state.tool_call_seq["worker[0]"] = 1
    state.tool_call_seq["asker"] = 1
    checkpoint = {
        "pending_toolcalls": [
            {"node_id": "worker[0]", "tool_call_id": "tc-1", "scoped_tool_call_id": None},
        ],
        "pending_agent_yields": [
            {"node_id": "asker", "tool_call_id": "tc-2", "scoped_tool_call_id": None},
        ],
    }

    node_tool_call_seq = stash_graph_scoped_ids(checkpoint, state)

    assert checkpoint["pending_toolcalls"][0]["scoped_tool_call_id"] == "worker[0]:tool:5:1"
    assert checkpoint["pending_agent_yields"][0]["scoped_tool_call_id"] == "asker:tool:5:1"
    assert node_tool_call_seq == {"worker[0]": 1, "asker": 1}


def test_stash_graph_scoped_ids_preserves_existing_value() -> None:
    """An entry carried forward from an earlier park (repark) already has a
    scoped_tool_call_id; a resume-time coalesce_state that never minted it
    must not overwrite it with a (missing) lookup."""
    from primer.session.persistence import _CoalesceState, stash_graph_scoped_ids

    state = _CoalesceState()  # fresh -- mints nothing for tc-1
    checkpoint = {
        "pending_toolcalls": [
            {
                "node_id": "worker[0]", "tool_call_id": "tc-1",
                "scoped_tool_call_id": "worker[0]:tool:3:1",
            },
        ],
        "pending_agent_yields": [],
    }

    stash_graph_scoped_ids(checkpoint, state)

    assert checkpoint["pending_toolcalls"][0]["scoped_tool_call_id"] == "worker[0]:tool:3:1"


def test_seeded_coalesce_state_avoids_scoped_id_collision_after_resume() -> None:
    """01a0690a piece 3 collision guard: turn_no does not bump across a
    park/resume (on_release only bumps it on a non-park release), so a
    resumed drain's fresh _CoalesceState observes the SAME turn_no the
    pre-park mints used. Without seeding tool_call_seq from the park's
    node_tool_call_seq snapshot, a node that made 2 calls before parking
    would mint seq 1, 2 again after resume -- a silent, unpaired id
    collision in the durable record.

    Simulates: node worker[0] makes 2 tool calls (minted live, pre-park),
    parks; on resume a FRESH _CoalesceState is seeded from the snapshot
    (mirroring _ResumeDrainTap.create) and the SAME node makes 2 more
    calls. All 4 scoped ids must be distinct.
    """
    from primer.model.chat import ToolCallStart
    from primer.session.persistence import _CoalesceState, translate_stream_event

    turn_no = 3
    node_id = "worker[0]"

    # --- pre-park: 2 tool calls on the live coalesce_state ---
    live_state = _CoalesceState()
    for i in range(2):
        translate_stream_event(
            ToolCallStart(id=f"raw-{i}", name="t", index=i), live_state,
            node_id=node_id, turn_no=turn_no,
        )
    pre_park_ids = {
        live_state.scoped_call_ids[(node_id, f"raw-{i}")] for i in range(2)
    }
    assert len(pre_park_ids) == 2  # sanity: the two pre-park mints differ

    # --- park: snapshot the mint-seq high-water mark (ParkedState.node_tool_call_seq) ---
    node_tool_call_seq = dict(live_state.tool_call_seq)

    # --- resume: a FRESH _CoalesceState, seeded per _ResumeDrainTap.create ---
    resume_state = _CoalesceState()
    resume_state.tool_call_seq = dict(node_tool_call_seq)
    for i in range(2, 4):
        translate_stream_event(
            ToolCallStart(id=f"raw-{i}", name="t", index=i - 2), resume_state,
            node_id=node_id, turn_no=turn_no,  # SAME turn_no as pre-park
        )
    post_resume_ids = {
        resume_state.scoped_call_ids[(node_id, f"raw-{i}")] for i in range(2, 4)
    }

    all_ids = pre_park_ids | post_resume_ids
    assert len(all_ids) == 4, f"expected 4 distinct scoped ids, got {all_ids}"


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
    assert r3[0].payload == {"text": "hi there", "part_id": "n1:text:0"}
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


def test_concurrent_nodes_tool_name_does_not_mix() -> None:
    """Two fan-out siblings whose tool-call ids collide (LLM adapters
    synthesize ids like "call_0" from a fresh per-stream counter, so two
    concurrent nodes legitimately emit the same id) must not clobber each
    other's stashed tool name. Interleaved A-start, B-start, A-end, B-end —
    each TOOL_CALL record must carry its OWN node's tool name."""
    from primer.model.chat import ToolCallEnd, ToolCallStart
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    same_id = "call_0"
    translate_stream_event(
        _wrap_node("a", ToolCallStart(id=same_id, name="fs__write", index=0)), state
    )
    translate_stream_event(
        _wrap_node("b", ToolCallStart(id=same_id, name="fs__read", index=0)), state
    )
    rec_a = translate_stream_event(
        _wrap_node("a", ToolCallEnd(id=same_id, arguments={"path": "a.txt"}, index=0)),
        state,
    )
    rec_b = translate_stream_event(
        _wrap_node("b", ToolCallEnd(id=same_id, arguments={"path": "b.txt"}, index=0)),
        state,
    )

    assert isinstance(rec_a, SessionMessageRecord)
    assert isinstance(rec_b, SessionMessageRecord)
    assert rec_a.node_id == "a"
    assert rec_a.payload["name"] == "fs__write"
    assert rec_b.node_id == "b"
    assert rec_b.payload["name"] == "fs__read"


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
    assert result[0].payload == {"text": "hello world", "part_id": "x:text:0"}
    assert result[0].node_id is None
    assert result[1].kind == SessionMessageKind.DONE
    assert result[1].node_id is None


def test_part_id_is_scoped_by_turn_so_a_second_turn_never_collides_with_the_first() -> None:
    """01a04e02: two SEPARATE turns (fresh _CoalesceState each, mirroring
    dispatch.run_one_session_turn's per-turn coalesce state - the two
    turns never share state, only the SAME session/node/kind) must
    produce DIFFERENT part_id values for their text part on the same
    node. Before this fix, part_id(node_id, kind) had no turn_no at all,
    so turn 2's assistant_token record would carry the EXACT same
    part_id as turn 1's - harmless on the backend (session-store.js is
    the one component that keeps a part alive across turns, see its own
    test), but a footgun waiting for the next consumer to trip over."""
    from primer.model.chat import Done, TextDelta
    from primer.session.persistence import _CoalesceState, translate_stream_event

    turn1_state = _CoalesceState()
    translate_stream_event(TextDelta(text="first turn", index=0), turn1_state, turn_no=1)
    turn1_result = translate_stream_event(
        Done(stop_reason="stop", raw_reason="stop"), turn1_state, turn_no=1,
    )
    turn1_token = next(
        r for r in turn1_result if r.kind == SessionMessageKind.ASSISTANT_TOKEN
    )

    turn2_state = _CoalesceState()
    translate_stream_event(TextDelta(text="second turn", index=0), turn2_state, turn_no=2)
    turn2_result = translate_stream_event(
        Done(stop_reason="stop", raw_reason="stop"), turn2_state, turn_no=2,
    )
    turn2_token = next(
        r for r in turn2_result if r.kind == SessionMessageKind.ASSISTANT_TOKEN
    )

    assert turn1_token.payload["part_id"] == "x:text:1"
    assert turn2_token.payload["part_id"] == "x:text:2"
    assert turn1_token.payload["part_id"] != turn2_token.payload["part_id"]


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


def test_graph_end_output_with_empty_text_writes_no_record() -> None:
    """Live finding 01a064d3, ruling (c): an End node with no/empty
    output_template renders "" - writing that as an ASSISTANT_TOKEN put
    an empty answer bubble into every graph transcript. Suppress at the
    source rather than persist noise."""
    from primer.graph.base import _GraphEndOutputEvent
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    rec = translate_stream_event(
        _GraphEndOutputEvent(text="", parsed=None, end_node_id="end1"), state
    )
    assert rec is None


def test_graph_end_output_suppresses_a_passthrough_duplicate() -> None:
    """Live finding 01a064d3, ruling (a): an End node whose
    output_template just echoes the immediately preceding node's answer
    (the common case) renders byte-identical text to that node's own
    ASSISTANT_TOKEN, already written this turn. The worker's answer IS
    the graph's result - a second record adds no information, only a
    duplicate paragraph in the transcript. Compare the final coalesced
    text (not deltas), scoped to the same turn_no."""
    from primer.model.chat import Done, TextDelta
    from primer.graph.base import _GraphEndOutputEvent
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    translate_stream_event(
        TextDelta(text="the worker's answer", index=0), state,
        node_id="worker", turn_no=3,
    )
    worker_done = translate_stream_event(
        Done(stop_reason="stop", raw_reason="stop"), state,
        node_id="worker", turn_no=3,
    )
    assert isinstance(worker_done, list)
    assert worker_done[0].kind == SessionMessageKind.ASSISTANT_TOKEN
    assert worker_done[0].payload["text"] == "the worker's answer"

    # Passthrough: byte-identical text, same turn -> suppressed.
    rec = translate_stream_event(
        _GraphEndOutputEvent(
            text="the worker's answer", parsed=None, end_node_id="end1",
        ),
        state, turn_no=3,
    )
    assert rec is None


def test_graph_end_output_keeps_a_genuine_transformation() -> None:
    """The other half of ruling (a): when output_template actually
    transforms the text (not a bare passthrough), the End record is
    semantically distinct from the worker's answer and must still be
    written - only a BYTE-IDENTICAL echo is noise."""
    from primer.model.chat import Done, TextDelta
    from primer.graph.base import _GraphEndOutputEvent
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    translate_stream_event(
        TextDelta(text="raw worker text", index=0), state,
        node_id="worker", turn_no=1,
    )
    translate_stream_event(
        Done(stop_reason="stop", raw_reason="stop"), state,
        node_id="worker", turn_no=1,
    )

    rec = translate_stream_event(
        _GraphEndOutputEvent(
            text="{summary: raw worker text}", parsed=None, end_node_id="end1",
        ),
        state, turn_no=1,
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.payload["text"] == "{summary: raw worker text}"


def test_graph_end_output_passthrough_check_is_scoped_to_the_same_turn() -> None:
    """The equality suppression only looks BACK within the same turn -
    an End node in a LATER turn that happens to render the same text as
    a PRIOR turn's last answer is not a duplicate of anything in this
    turn's transcript and must still be written."""
    from primer.model.chat import Done, TextDelta
    from primer.graph.base import _GraphEndOutputEvent
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    translate_stream_event(
        TextDelta(text="same text", index=0), state, node_id="worker", turn_no=1,
    )
    translate_stream_event(
        Done(stop_reason="stop", raw_reason="stop"), state,
        node_id="worker", turn_no=1,
    )

    rec = translate_stream_event(
        _GraphEndOutputEvent(text="same text", parsed=None, end_node_id="end1"),
        state, turn_no=2,
    )
    assert isinstance(rec, SessionMessageRecord)
    assert rec.payload["text"] == "same text"


def test_translate_reasoning_delta_coalesces_and_flushes_before_text() -> None:
    """ReasoningDeltas coalesce; Done flushes thought BEFORE the answer.

    Until 2026-08-25 ReasoningDelta was silently dropped, so thinking
    never reached the transcript at all.
    """
    from primer.model.chat import Done, ReasoningDelta, TextDelta
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    assert translate_stream_event(
        ReasoningDelta(text="let me ", index=0), state) is None
    assert translate_stream_event(
        ReasoningDelta(text="think", index=0), state) is None
    assert translate_stream_event(
        TextDelta(text="the answer", index=0), state) is None
    result = translate_stream_event(
        Done(stop_reason="stop", raw_reason="stop"), state)

    assert isinstance(result, list)
    assert [r.kind for r in result] == [
        SessionMessageKind.REASONING,
        SessionMessageKind.ASSISTANT_TOKEN,
        SessionMessageKind.DONE,
    ]
    assert result[0].payload == {"text": "let me think", "part_id": "x:reasoning:0"}
    assert result[1].payload == {"text": "the answer", "part_id": "x:text:0"}
    # Done cleared the buffer: a stray second Done replays nothing.
    assert state.reasoning_buffers == {}


def test_translate_tool_call_end_flushes_reasoning_first() -> None:
    """ToolCallEnd orders thought -> buffered text -> the tool call."""
    from primer.model.chat import ReasoningDelta, ToolCallEnd
    from primer.session.persistence import _CoalesceState, translate_stream_event

    state = _CoalesceState()
    translate_stream_event(ReasoningDelta(text="need a file", index=0), state)
    result = translate_stream_event(
        ToolCallEnd(id="call_0", arguments={"path": "x"}, index=0), state)

    assert isinstance(result, list)
    assert [r.kind for r in result] == [
        SessionMessageKind.REASONING,
        SessionMessageKind.TOOL_CALL,
    ]
    assert result[0].payload == {"text": "need a file", "part_id": "x:reasoning:0"}
