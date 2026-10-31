"""The tap reads only the bytes a session log gained since its last tick (perf plan Task 13, finding M6).

``read_session_since`` fetched the whole ``messages.jsonl`` every tick and sliced off what it had seen, so a long session cost O(file) per tick and
O(n^2) bytes in all. A workspace whose IO offers ``read_file_from(path, offset)`` is asked for the tail only; one that does not is read whole, as
before. The events and the next offset are identical either way (pinned here on a log that grows through partial lines).
"""
from __future__ import annotations

import json

import pytest

from primer.model.except_ import NotFoundError
from primer.tap.reader import read_session_since
from primer.tap.selector import TapSelector
from datetime import UTC, datetime

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession


def _msg_path(session_id: str) -> str:
    return f".state/sessions/{session_id}/messages.jsonl"


def _session(sid: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING,
        created_at=datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC), turn_status="idle",
    )


def _line(seq: int) -> bytes:
    return (json.dumps({"seq": seq, "kind": "assistant_token", "payload": {"text": f"t{seq}"}, "created_at": "2026-06-30T12:00:00+00:00"}) + "\n").encode()


class _WholeFileIO:
    state_path = ".state"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.bytes_read = 0

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise NotFoundError(path)
        self.bytes_read += len(self.files[path])
        return self.files[path]


class _RangedIO(_WholeFileIO):
    async def read_file_from(self, path: str, offset: int) -> bytes:
        if path not in self.files:
            raise NotFoundError(path)
        tail = self.files[path][offset:]
        self.bytes_read += len(tail)
        return tail


async def _drain(io, ticks: list[bytes], session):
    """Append ``ticks`` one by one, reading after each; return (all seqs seen, final offset)."""
    seen: list[int] = []
    offset = 0
    last_seq = 0
    for chunk in ticks:
        io.files[_msg_path(session.id)] = io.files.get(_msg_path(session.id), b"") + chunk
        events, offset = await read_session_since(
            io, workspace_id="w1", session=session, after_seq=last_seq, selector=TapSelector(), from_offset=offset,
        )
        for ev in events:
            seen.append(ev.seq)
            last_seq = max(last_seq, ev.seq)
    return seen, offset


GROWTH = [_line(1) + _line(2), _line(3)[:20], _line(3)[20:] + _line(4), b"", _line(5)]  # includes a partial line and an empty tick


@pytest.mark.asyncio
async def test_a_ranged_io_and_a_whole_file_io_give_the_same_events_and_offset():
    session = _session("s1")
    whole, ranged = _WholeFileIO(), _RangedIO()
    assert await _drain(ranged, GROWTH, session) == await _drain(whole, GROWTH, session) == ([1, 2, 3, 4, 5], sum(len(c) for c in GROWTH))


@pytest.mark.asyncio
async def test_a_missing_log_is_empty_on_both():
    for io in (_WholeFileIO(), _RangedIO()):
        events, offset = await read_session_since(io, workspace_id="w1", session=_session("s1"), after_seq=0, selector=TapSelector(), from_offset=7)
        assert (events, offset) == ([], 7)


@pytest.mark.asyncio
async def test_a_log_that_shrank_below_the_offset_reads_nothing_and_keeps_the_offset():
    session = _session("s1")
    for io in (_WholeFileIO(), _RangedIO()):
        io.files[_msg_path(session.id)] = _line(1)
        events, offset = await read_session_since(
            io, workspace_id="w1", session=session, after_seq=0, selector=TapSelector(), from_offset=10_000,
        )
        assert (events, offset) == ([], 10_000)


@pytest.mark.asyncio
async def test_a_ranged_io_reads_only_the_new_bytes_each_tick():
    session = _session("s1")
    ticks = [_line(i) for i in range(1, 101)]
    whole, ranged = _WholeFileIO(), _RangedIO()
    await _drain(whole, ticks, session)
    await _drain(ranged, ticks, session)
    total = sum(len(t) for t in ticks)
    assert ranged.bytes_read == total, "each byte of the log is fetched once"
    assert whole.bytes_read > 20 * total, "the whole-file read is quadratic"
