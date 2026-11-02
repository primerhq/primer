"""The tap reads a segmented session log with LOGICAL offsets (segmented session log, design D6 and section 5.3).

An offset a reader holds (the live path's ``next_offset``, a cursor's ``offsets``) is into the concatenation of the segments, so it stays valid when the log
rotates: nothing is skipped and nothing repeats. The log here is an in-memory workspace IO serving ``messages.jsonl``, ``messages.0001.jsonl`` ... in a session slot.
"""

from __future__ import annotations

import pytest

from primer.claim.tool_call_lookup import ToolCallRecordMismatch, read_tool_call_record
from primer.tap.cursor import TapCursor
from primer.tap.reader import read_batch, read_record_by_seq, read_session_since
from primer.tap.selector import TapSelector
from tests.claim.test_tool_call_lookup import _make_task
from tests.tap.test_reader import _NOW, _FakeWorkspaceIO, _line, _Provider, _seed_session


def _seg(sid: str, n: int) -> str:
    return f".state/sessions/{sid}/" + ("messages.jsonl" if n == 0 else f"messages.{n:04d}.jsonl")


def _seqs(events) -> list[int]:
    return [e.seq for e in events]


async def _since(io, session, after_seq: int, offset: int):
    return await read_session_since(io, workspace_id="ws-1", session=session, after_seq=after_seq, selector=TapSelector(), from_offset=offset)


async def _session(io: _FakeWorkspaceIO, sid: str = "s1"):
    provider = _Provider()
    await _seed_session(provider.store, sid, agent_id="ag1")
    return provider, (await provider.store.get(sid))


@pytest.mark.asyncio
async def test_live_tick_across_a_rotation_loses_no_event_and_repeats_none() -> None:
    io = _FakeWorkspaceIO()
    _, session = await _session(io)
    io.write(_seg("s1", 0), _line(1, "user_input") + _line(2, "tool_call"))
    events, offset = await _since(io, session, 0, 0)
    assert _seqs(events) == [1, 2]
    # the log rotates: segment 0 keeps its bytes, a new active segment holds what comes next
    io.append(_seg("s1", 0), _line(3, "assistant_token"))
    io.write(_seg("s1", 1), _line(4, "tool_result") + _line(5, "done"))
    events, offset = await _since(io, session, 2, offset)
    assert _seqs(events) == [3, 4, 5], "the tail of segment 0 and all of segment 1, once"
    io.append(_seg("s1", 1), _line(6, "user_input"))
    events, offset2 = await _since(io, session, 5, offset)
    assert _seqs(events) == [6] and offset2 > offset
    assert await _since(io, session, 6, offset2) == ([], offset2)


@pytest.mark.asyncio
async def test_a_partial_last_line_in_the_new_segment_is_left_for_the_next_tick() -> None:
    io = _FakeWorkspaceIO()
    _, session = await _session(io)
    io.write(_seg("s1", 0), _line(1, "user_input"))
    _, offset = await _since(io, session, 0, 0)
    whole = _line(3, "done")
    io.write(_seg("s1", 1), _line(2, "tool_call") + whole[:10])
    events, mid = await _since(io, session, 1, offset)
    assert _seqs(events) == [2]
    io.append(_seg("s1", 1), whole[10:])
    events, _ = await _since(io, session, 2, mid)
    assert _seqs(events) == [3]


@pytest.mark.asyncio
async def test_read_batch_with_a_stored_offset_from_before_the_rotation() -> None:
    io = _FakeWorkspaceIO()
    provider, _ = await _session(io)
    io.write(_seg("s1", 0), _line(1, "user_input") + _line(2, "tool_call") + _line(3, "tool_result"))
    cursor = TapCursor(seqs={}, known_as_of=_NOW)
    events, cursor = await read_batch(provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=2)
    assert _seqs(events) == [1, 2], "the limit cuts segment 0 mid-way; the cursor holds a logical offset into it"
    stored = cursor.offsets["s1"]
    io.write(_seg("s1", 1), _line(4, "assistant_token") + _line(5, "done"))
    events, cursor = await read_batch(provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=100)
    assert _seqs(events) == [3, 4, 5], "the resume reads the rest of segment 0, then segment 1"
    assert cursor.offsets["s1"] > stored
    events, _ = await read_batch(provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=100)
    assert events == []


@pytest.mark.asyncio
async def test_the_cursor_token_is_unchanged_by_segmentation() -> None:
    io = _FakeWorkspaceIO()
    provider, _ = await _session(io)
    io.write(_seg("s1", 0), _line(1, "user_input"))
    io.write(_seg("s1", 1), _line(2, "done"))
    cursor = TapCursor(seqs={}, known_as_of=_NOW)
    _, cursor = await read_batch(provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=100)
    assert set(cursor.offsets) == {"s1"} and cursor.seqs == {"s1": 2}
    assert cursor.offsets["s1"] == len(_line(1, "user_input")) + len(_line(2, "done")), "the logical offset is the single file's length"
    assert TapCursor.decode(cursor.encode()) == cursor


@pytest.mark.asyncio
async def test_read_record_by_seq_finds_a_record_in_every_segment() -> None:
    io = _FakeWorkspaceIO()
    io.write(_seg("s1", 0), _line(1, "user_input") + _line(2, "tool_call") + _line(3, "tool_result"))
    io.write(_seg("s1", 1), _line(4, "assistant_token") + _line(5, "done"))
    io.write(_seg("s1", 2), _line(6, "user_input") + _line(7, "tool_call") + _line(8, "done"))
    for seq in range(1, 9):
        record = await read_record_by_seq(io, session_id="s1", seq=seq)
        assert record is not None and record.seq == seq, seq
    assert await read_record_by_seq(io, session_id="s1", seq=9) is None
    assert await read_record_by_seq(io, session_id="s1", seq=0) is None
    assert await read_record_by_seq(io, session_id="nope", seq=1) is None


@pytest.mark.asyncio
async def test_read_record_by_seq_stops_at_the_segment_that_starts_at_or_below_the_target() -> None:
    io = _FakeWorkspaceIO()
    io.write(_seg("s1", 0), _line(1, "user_input") + _line(2, "tool_call"))
    io.write(_seg("s1", 1), _line(3, "tool_result") + _line(4, "done"))
    io.write(_seg("s1", 2), _line(5, "user_input"))
    io.read_calls = 0
    assert (await read_record_by_seq(io, session_id="s1", seq=4)).seq == 4
    assert io.read_calls == 2, "the active segment, then the one that holds it: segment 0 is never read"
    io.read_calls = 0
    assert await read_record_by_seq(io, session_id="s1", seq=3) is not None
    assert io.read_calls == 2
    io.read_calls = 0
    assert await read_record_by_seq(io, session_id="s1", seq=99) is None
    assert io.read_calls == 1, "the newest segment starts below the target and does not hold it: nothing older can"


@pytest.mark.asyncio
async def test_read_record_by_seq_walks_past_a_segment_of_bare_messages() -> None:
    io = _FakeWorkspaceIO()
    io.write(_seg("s1", 0), _line(1, "user_input") + _line(2, "tool_call"))
    io.write(_seg("s1", 1), b'{"role": "user", "parts": []}\n')
    assert (await read_record_by_seq(io, session_id="s1", seq=2)).seq == 2


@pytest.mark.asyncio
async def test_tool_call_lookup_across_a_rotation() -> None:
    io = _FakeWorkspaceIO()
    io.write(_seg("sess-1", 0), _line(1, "user_input") + _line(2, "tool_call", id="w:tool:1:1", name="workspace__write"))
    io.write(_seg("sess-1", 1), _line(3, "tool_result") + _line(4, "done"))
    record = await read_tool_call_record(io, _make_task(record_seq=2))
    assert record.seq == 2 and record.payload["name"] == "workspace__write"
    with pytest.raises(ToolCallRecordMismatch, match="no record"):
        await read_tool_call_record(io, _make_task(record_seq=9))


@pytest.mark.asyncio
async def test_backfill_after_seq_zero_reads_every_segment() -> None:
    """The workspace tap backfill (``after_seq=0`` from offset 0 per session) is ``read_session_since`` over the whole log."""
    io = _FakeWorkspaceIO()
    _, session = await _session(io)
    io.write(_seg("s1", 0), _line(1, "user_input") + _line(2, "tool_call"))
    io.write(_seg("s1", 1), _line(3, "tool_result"))
    io.write(_seg("s1", 2), _line(4, "done"))
    events, offset = await _since(io, session, 0, 0)
    assert _seqs(events) == [1, 2, 3, 4]
    assert offset == sum(len(v) for v in io._files.values())
