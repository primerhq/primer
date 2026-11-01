"""T117 (GS-18): a new turn-log writer seeds its seq from the file's tail, not by parsing the whole log on the loop.

The pool builds a writer per claim, so every turn used to read the whole ``turns.jsonl`` and parse every line on the event loop.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import pytest

from primer.model.except_ import NotFoundError
from primer.model.turn_log import TurnLogStarted
from primer.observability import turn_log_writer
from primer.observability.turn_log_writer import WorkspaceTurnLogWriter


def _line(seq: int) -> bytes:
    return (json.dumps({"seq": seq, "ts": "2026-10-31T00:00:00Z", "kind": "phase", "phase": "thinking", "turn_no": 1}) + "\n").encode()


class _File:
    """A workspace double that counts the bytes handed out."""

    def __init__(self, data: bytes | None):
        self.data, self.bytes_read, self.full_reads, self.written = data, 0, 0, []

    async def append(self, line: bytes) -> None:
        self.written.append(line)

    async def read_existing(self) -> bytes:
        if self.data is None:
            raise NotFoundError("turns.jsonl")
        self.full_reads += 1
        self.bytes_read += len(self.data)
        return self.data

    async def read_tail(self, max_bytes: int):
        if self.data is None:
            raise NotFoundError("turns.jsonl")
        tail = self.data[-max_bytes:]
        self.bytes_read += len(tail)
        return tail, len(tail) == len(self.data)


def _event():
    return TurnLogStarted(seq=0, ts=datetime.now(UTC))


async def _first_seq(file: _File, *, tail: bool) -> int:
    w = WorkspaceTurnLogWriter(
        append_line=file.append, read_existing=file.read_existing, read_tail=file.read_tail if tail else None,
    )
    return await w.append(_event())


CASES = {
    "missing": None,
    "empty": b"",
    "short": b"".join(_line(i) for i in range(1, 4)),
    "partial_last_line": b"".join(_line(i) for i in range(1, 6)) + b'{"seq": 6, "ts": "2026',
    "long": b"".join(_line(i) for i in range(1, 5001)),
    "tail_all_garbage_then_lines": b"".join(_line(i) for i in range(1, 50)) + b"x" * 200_000,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(CASES))
async def test_seq_numbering_is_the_same_with_and_without_the_tail_read(name):
    assert await _first_seq(_File(CASES[name]), tail=True) == await _first_seq(_File(CASES[name]), tail=False)


@pytest.mark.asyncio
async def test_a_long_log_reads_only_its_tail_and_parses_nothing_on_the_loop(monkeypatch):
    history = b"".join(_line(i) for i in range(1, 150_001))                    # ~13 MB
    file = _File(history)
    parsed_on = []
    real = json.loads

    def spy(s, *a, **k):
        parsed_on.append(threading.current_thread() is threading.main_thread())
        return real(s, *a, **k)

    monkeypatch.setattr(turn_log_writer.json, "loads", spy)
    assert await _first_seq(file, tail=True) == 150_001
    assert file.full_reads == 0 and file.bytes_read <= 64 * 1024 + len(_line(150_000))
    assert len(parsed_on) <= 3 and not any(parsed_on)


@pytest.mark.asyncio
async def test_a_tail_without_a_parsable_line_falls_back_to_the_full_scan_off_the_loop(monkeypatch):
    file = _File(CASES["tail_all_garbage_then_lines"])
    parsed_on = []
    real = json.loads
    monkeypatch.setattr(turn_log_writer.json, "loads", lambda s, *a, **k: (parsed_on.append(threading.current_thread() is threading.main_thread()), real(s, *a, **k))[1])
    assert await _first_seq(file, tail=True) == 50
    assert file.full_reads == 1 and not any(parsed_on)


@pytest.mark.asyncio
async def test_a_failed_tail_read_propagates_and_is_retried():
    calls = 0

    async def tail(n):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("boom")
        return _line(7), True

    f = _File(b"")
    w = WorkspaceTurnLogWriter(append_line=f.append, read_existing=f.read_existing, read_tail=tail)
    with pytest.raises(OSError):
        await w.append(_event())
    assert f.written == [] and await w.append(_event()) == 8
