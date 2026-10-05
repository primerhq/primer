"""WorkspaceMessageWriter.reserve_seq: a seq for a record somebody else writes, without a collision.

A compaction marker is written by the executor straight into the log. If it took "the file's next seq" while
records the writer had already numbered were still buffered, the writer's next record would repeat it, and the
marker would sit in the file before events with lower seqs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.persistence import WorkspaceMessageWriter


class _IO:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append(line)


def _rec() -> SessionMessageRecord:
    return SessionMessageRecord(seq=1, kind=SessionMessageKind.ASSISTANT_TOKEN, payload={"text": "t"}, created_at=datetime.now(UTC))


def _seqs(io: _IO) -> list[int]:
    return [json.loads(line)["seq"] for blob in io.lines for line in blob.decode().splitlines() if line.strip()]


async def test_the_buffered_records_are_flushed_before_the_reserved_seq_is_handed_out() -> None:
    io = _IO()
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s", start_seq=10)
    for _ in range(3):
        await writer.append(_rec())
    assert io.lines == [], "still buffered"
    seq = await writer.reserve_seq()
    assert seq == 14 and _seqs(io) == [11, 12, 13], "the file holds everything numbered so far, in order"


async def test_the_next_record_continues_after_the_reserved_seq_so_it_never_repeats_it() -> None:
    io = _IO()
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s", start_seq=0)
    await writer.append(_rec())
    reserved = await writer.reserve_seq()
    assert await writer.append(_rec()) == reserved + 1
    assert writer.last_seq == reserved + 1, "the turn's last_seq (persisted on the row) covers the marker"


async def test_reserving_with_nothing_buffered_just_advances_the_counter() -> None:
    io = _IO()
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s", start_seq=5)
    assert await writer.reserve_seq() == 6 and io.lines == []
