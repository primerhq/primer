"""Review 2026-11-23 4.5 / 6.5: a tap's first tick and the events backfill read the END of a session log, not all of it from byte 0."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

import primer.tap.reader as reader
from primer.model.except_ import NotFoundError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.tap.reader import read_session_since
from primer.tap.selector import TapSelector
from tests._support.segmented_io import ListsFiles

_BASE = datetime(2026, 6, 1, tzinfo=UTC)
_PATH = ".state/sessions/s1/messages.jsonl"


def _line(seq: int, pad: int = 100) -> bytes:
    rec = {"seq": seq, "kind": "assistant_token", "payload": {"text": "x" * pad},
           "created_at": (_BASE + timedelta(seconds=seq)).isoformat()}
    return (json.dumps(rec) + "\n").encode()


class _IO(ListsFiles):
    state_path = ".state"

    def __init__(self, data: bytes) -> None:
        self.files = {_PATH: data}
        self.bytes_read = 0

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise NotFoundError(path)
        self.bytes_read += len(self.files[path])
        return self.files[path]

    async def read_file_from(self, path: str, offset: int) -> bytes:
        if path not in self.files:
            raise NotFoundError(path)
        tail = self.files[path][offset:]
        self.bytes_read += len(tail)
        return tail


def _session() -> WorkspaceSession:
    return WorkspaceSession(
        id="s1", workspace_id="w", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING,
        created_at=_BASE, turn_status="idle",
    )


N = 4000  # ~ 500 KB: far past the 64 KB first window
LOG = b"".join(_line(i) for i in range(1, N + 1))


async def _read(io, **kw):
    return await read_session_since(io, workspace_id="w", session=_session(), selector=TapSelector(), **kw)


@pytest.mark.asyncio
async def test_first_tick_with_a_resume_seq_reads_only_the_tail_and_matches_a_full_read():
    full_events, full_offset = await _read(_IO(LOG), after_seq=0)
    io = _IO(LOG)
    events, offset = await _read(io, after_seq=N - 5)
    assert [e.seq for e in events] == [N - 4, N - 3, N - 2, N - 1, N]
    assert offset == full_offset == len(LOG)
    assert [e.seq for e in full_events] == list(range(1, N + 1))
    assert io.bytes_read < len(LOG) // 4, "the log was read from byte 0"


@pytest.mark.asyncio
async def test_the_window_widens_until_it_reaches_the_resume_seq():
    io = _IO(LOG)
    events, offset = await _read(io, after_seq=N - 2500)  # ~300 KB of new records: several doublings
    assert [e.seq for e in events] == list(range(N - 2499, N + 1))
    assert offset == len(LOG)


@pytest.mark.asyncio
async def test_a_resume_point_older_than_the_log_reads_everything():
    io = _IO(LOG)
    events, _ = await _read(io, after_seq=0, since=_BASE - timedelta(days=1))
    assert len(events) == N


@pytest.mark.asyncio
async def test_since_bounds_the_window_for_a_session_the_cursor_never_named():
    io = _IO(LOG)
    since = _BASE + timedelta(seconds=N - 10)
    events, _ = await _read(io, after_seq=0, since=since)
    assert [e.seq for e in events if e.ts > since] == list(range(N - 9, N + 1))
    assert io.bytes_read < len(LOG) // 4


@pytest.mark.asyncio
async def test_a_first_read_of_a_new_session_still_reads_from_byte_0():
    io = _IO(_line(1) + _line(2))
    events, offset = await _read(io, after_seq=0)
    assert [e.seq for e in events] == [1, 2] and offset == len(io.files[_PATH])


@pytest.mark.asyncio
async def test_the_next_tick_continues_from_the_returned_offset():
    io = _IO(LOG)
    _, offset = await _read(io, after_seq=N - 5)
    io.files[_PATH] += _line(N + 1)
    events, offset2 = await _read(io, after_seq=N, from_offset=offset)
    assert [e.seq for e in events] == [N + 1] and offset2 == len(io.files[_PATH])


@pytest.mark.asyncio
async def test_a_partial_trailing_line_is_left_for_the_next_tick():
    io = _IO(LOG + _line(N + 1)[:30])
    events, offset = await _read(io, after_seq=N - 1)
    assert [e.seq for e in events] == [N] and offset == len(LOG)


@pytest.mark.asyncio
async def test_backfill_tail_returns_only_the_newest_records_from_the_end():
    io = _IO(LOG)
    events, _ = await _read(io, after_seq=0, tail_limit=50)
    assert [e.seq for e in events] == list(range(N - 49, N + 1))
    assert io.bytes_read < len(LOG) // 4


@pytest.mark.asyncio
async def test_backfill_tail_of_a_short_log_returns_all_of_it():
    events, _ = await _read(_IO(_line(1) + _line(2)), after_seq=0, tail_limit=50)
    assert [e.seq for e in events] == [1, 2]


@pytest.mark.asyncio
async def test_backfill_never_reads_more_than_its_cap(monkeypatch):
    monkeypatch.setattr(reader, "BACKFILL_MAX_BYTES", 256 * 1024)
    io = _IO(b"".join(_line(i, pad=20_000) for i in range(1, 200)))  # records of 20 KB: 50 of them are 1 MB
    events, _ = await _read(io, after_seq=0, tail_limit=50)
    assert 0 < len(events) < 50
    assert io.bytes_read <= 512 * 1024


@pytest.mark.asyncio
async def test_a_big_window_is_parsed_off_the_event_loop(monkeypatch):
    main = threading.get_ident()
    seen: list[int] = []
    real = reader._parse_lines

    def spy(lines, after_seq):
        seen.append(threading.get_ident())
        return real(lines, after_seq)

    monkeypatch.setattr(reader, "_parse_lines", spy)
    await _read(_IO(LOG), after_seq=0)  # the whole 500 KB log
    assert seen and all(t != main for t in seen)
    seen.clear()
    await _read(_IO(_line(1)), after_seq=0)  # a live tick's one line stays inline
    assert seen == [main]
