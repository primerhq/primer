"""Review round 1 (I2, M-1/M-2): a newest record that alone exceeds the backfill cap does not make the session
disappear, and the tail ``covers`` check parses only the window's first line."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.tap.reader import BACKFILL_MAX_BYTES, _first_record_at_or_before, read_session_since
from primer.tap.selector import TapSelector
from tests._support.segmented_io import ListsFiles

_BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)
_PATH = ".state/sessions/s1/messages.jsonl"


class _IO(ListsFiles):
    state_path = ".state"

    def __init__(self, data: bytes) -> None:
        self.files = {_PATH: data}

    async def read_file(self, path):
        if path not in self.files:
            raise NotFoundError(path)
        return self.files[path]

    async def read_file_from(self, path, offset):
        if path not in self.files:
            raise NotFoundError(path)
        return self.files[path][offset:]


def _sess() -> WorkspaceSession:
    return WorkspaceSession(
        id="s1", workspace_id="w", binding=AgentSessionBinding(agent_id="a"),
        status=SessionStatus.RUNNING, created_at=_BASE, turn_status="idle",
    )


def _rec(seq: int, text: str) -> bytes:
    return (
        json.dumps(
            {"seq": seq, "kind": "assistant_token", "payload": {"text": text},
             "created_at": (_BASE + timedelta(seconds=seq)).isoformat()}
        )
        + "\n"
    ).encode()


@pytest.mark.asyncio
async def test_a_backfill_whose_newest_record_outruns_the_cap_still_returns_the_earlier_records():
    """M-1: the old window was the last 4 MiB -- all one line -- and parsed to 0 records. Now the window ends before
    the giant record, the records before it count, and the returned offset sits at the giant record's start (a live
    read from it picks the giant one up once it is whole)."""
    small = b"".join(_rec(s, "x" * 100) for s in range(1, 300))
    giant = _rec(300, "y" * (5 * 1024 * 1024))
    log = small + giant
    assert len(small) < BACKFILL_MAX_BYTES < len(log)

    events, offset = await read_session_since(
        _IO(log), workspace_id="w", session=_sess(), after_seq=0,
        selector=TapSelector(), tail_limit=200,
    )
    assert [e.seq for e in events] == list(range(100, 300)), "the newest 200 of the 299 earlier records, not 0"
    assert offset == len(small)


@pytest.mark.asyncio
async def test_a_backfill_with_a_partial_giant_tail_still_returns_the_earlier_records():
    """M-1 (the unflushed form): the giant record has no trailing newline yet -- the same fix ends the window before it."""
    small = b"".join(_rec(s, "x" * 100) for s in range(1, 300))
    giant = _rec(300, "y" * (5 * 1024 * 1024))[: 5 * 1024 * 1024]  # truncated: no closing newline
    events, offset = await read_session_since(
        _IO(small + giant), workspace_id="w", session=_sess(), after_seq=0,
        selector=TapSelector(), tail_limit=200,
    )
    assert [e.seq for e in events] == list(range(100, 300))
    assert offset == len(small)


def test_covers_decides_on_the_first_line_only():
    """M-2: only the bytes up to the first newline are parsed (a later record can only be newer)."""
    first, second = _rec(1, "a"), _rec(2, "b")
    assert _first_record_at_or_before(first + second, after_seq=1, since=None)
    assert not _first_record_at_or_before(first + second, after_seq=0, since=None)
    # A first line that is not a record decides on its own (the older record after it no longer hides the gap).
    assert not _first_record_at_or_before(b"not json\n" + first, after_seq=1, since=None)
