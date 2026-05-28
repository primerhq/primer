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

from matrix.model.workspace_session import SessionMessageKind, SessionMessageRecord
from matrix.session.persistence import WorkspaceMessageWriter


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
