"""A cancel of the task that flushes the message writer must not lose records (task D4, found in the 01a10e2f audit).

``WorkspaceMessageWriter._do_flush`` took the batch out of its buffer and THEN awaited the append. A cancel in that await
(waiting for ``messages_lock`` behind a git commit, or in the thread hop) therefore dropped the batch: it was no longer in
the buffer and was never written. The writer is shared (the delegation recorder writes the subagent's records through the
same one as the parent turn), and a Stop's cancel of a call, or a hard Cancel, can land in any flush.

What is pinned here, as the file would show it:

* once a batch has left the buffer it IS written, exactly once, even if the flushing task is cancelled;
* a later flush does not overtake it (the file stays in seq order);
* a record is never dropped by a cancel that lands in the flush an ``append`` triggers (age policy);
* an append that FAILS still reaches the task that flushed, as before.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.persistence import WorkspaceMessageWriter


def _record(text: str = "x") -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=1, kind=SessionMessageKind.ASSISTANT_TOKEN, payload={"text": text}, created_at=datetime.now(timezone.utc),
    )


class _GatedIO:
    """``append_message_line`` that blocks until released, as the real one does while it waits for ``messages_lock``.

    ``block_first_only`` lets every call after the first go straight through, so a later flush can try to overtake."""

    def __init__(self, *, block_first_only: bool = False) -> None:
        self.release = asyncio.Event()
        self.block_first_only = block_first_only
        self.started: list[bytes] = []
        self.landed: list[bytes] = []
        self.fail_with: BaseException | None = None

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.started.append(line)
        if not (self.block_first_only and len(self.started) > 1):
            await self.release.wait()
        if self.fail_with is not None:
            raise self.fail_with
        self.landed.append(line)

    def texts(self) -> list[str]:
        """The record texts in the order they landed in the file."""
        out: list[str] = []
        for chunk in self.landed:
            out.extend(json.loads(line)["payload"]["text"] for line in chunk.decode().splitlines() if line.strip())
        return out


async def _spin(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def test_a_flush_cancelled_while_the_append_waits_still_lands_the_batch_exactly_once() -> None:
    io = _GatedIO()
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
    await writer.append(_record("a"))
    await writer.append(_record("b"))

    flushing = asyncio.create_task(writer.flush())
    await _spin()
    assert len(io.started) == 1, "the flush did not reach the append: the test is not in its situation"
    flushing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flushing

    io.release.set()                                       # the lock is free, the write goes through
    await _spin()

    assert io.texts() == ["a", "b"], f"the batch was lost or doubled: {io.texts()}"
    assert len(io.started) == 1


async def test_a_later_flush_does_not_overtake_the_batch_of_a_cancelled_one() -> None:
    io = _GatedIO(block_first_only=True)
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
    await writer.append(_record("a"))
    first = asyncio.create_task(writer.flush())
    await _spin()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    await writer.append(_record("b"))
    second = asyncio.create_task(writer.flush())
    await _spin()
    assert io.landed == [], "the later flush wrote while the earlier batch was still in flight"

    io.release.set()
    await asyncio.wait_for(second, timeout=5.0)

    assert io.texts() == ["a", "b"], f"the file is out of seq order or lost a record: {io.texts()}"


async def test_a_cancel_in_the_flush_an_append_triggers_keeps_the_new_record() -> None:
    """The age policy flushes inside ``append``: the record being appended has its seq and must not be dropped with the cancel."""
    io = _GatedIO()
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
    await writer.append(_record("old"))
    await asyncio.sleep(0.15)                              # the buffered record is older than the 100 ms policy

    appending = asyncio.create_task(writer.append(_record("new")))
    await _spin()
    assert len(io.started) == 1, "the append did not trigger the age flush: the test is not in its situation"
    appending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await appending

    io.release.set()
    await _spin()
    await writer.flush()

    assert io.texts() == ["old", "new"], f"a record was lost to the cancel: {io.texts()}"
    assert writer.last_seq == 2


async def test_an_append_that_fails_still_reaches_the_task_that_flushed() -> None:
    io = _GatedIO()
    io.release.set()
    io.fail_with = OSError("disk is gone")
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
    await writer.append(_record("a"))

    with pytest.raises(OSError, match="disk is gone"):
        await writer.flush()


async def test_flushes_in_a_row_without_a_cancel_write_each_batch_once_in_order() -> None:
    io = _GatedIO()
    io.release.set()
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
    await writer.append(_record("a"))
    await writer.flush()
    await writer.append(_record("b"))
    await writer.append(_record("c"))
    await writer.aclose()
    await writer.flush()                                   # nothing buffered: no write

    assert io.texts() == ["a", "b", "c"] and len(io.started) == 2
