"""A batch the workspace never answers must not hold the message writer, or the turn exit behind it (ticket 01a11b58).

``WorkspaceMessageWriter._do_flush`` keeps the append of a batch in flight as its own task, awaits it behind a shield, and makes every
later flush wait for it so the file stays in seq order. When the workspace's runtime connection drops, that append never returns: the
flush waits for it without limit, and so does every flush after it.

What is pinned here, as the writer and the file would show it:

* a flush stops waiting for a batch that has been in flight longer than the write bound, and raises ``WorkspaceWriteTimeout`` (a
  ``TimeoutError``); the loss is logged;
* the writer is then BROKEN for the rest of its life: it starts no further write and its appends and flushes fail at once, so nothing
  it holds can overtake the abandoned batch in the file (the next turn builds a new writer, which probes the workspace afresh);
* the abandoned batch is never re-sent: if the workspace answers late it is in the file exactly once, and the records that were
  buffered behind it are never written;
* a healthy slow write is not abandoned, and a write that FAILS still raises as itself.

Every test body is bounded: no path may hang the lane.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session import persistence
from primer.session.persistence import WorkspaceMessageWriter

HARD_BOUND_S = 5.0     # the whole body of a test
WRITE_BOUND_S = 0.2    # the writer's bound, far under the hard one


def _record(text: str = "x") -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=1, kind=SessionMessageKind.ASSISTANT_TOKEN, payload={"text": text}, created_at=datetime.now(timezone.utc),
    )


class _Workspace:
    """``append_message_line`` that does not answer until ``release`` is set (never, for a workspace whose connection dropped)."""

    def __init__(self, *, answers_after: float | None = None) -> None:
        self.release = asyncio.Event()
        self.started: list[bytes] = []
        self.landed: list[bytes] = []
        self.answers_after = answers_after
        self.fail_with: BaseException | None = None

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.started.append(line)
        if self.answers_after is not None:
            await asyncio.sleep(self.answers_after)
        else:
            await self.release.wait()
        if self.fail_with is not None:
            raise self.fail_with
        self.landed.append(line)

    def texts(self) -> list[str]:
        out: list[str] = []
        for chunk in self.landed:
            out.extend(json.loads(line)["payload"]["text"] for line in chunk.decode().splitlines() if line.strip())
        return out


async def _spin(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


@pytest.fixture
def bounded_writes(monkeypatch):
    # raising=False: before the bound exists the setting is inert and the tests below fail by hanging, which the body bound reports
    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", WRITE_BOUND_S, raising=False)


async def test_a_flush_gives_up_on_a_batch_that_never_returns(bounded_writes, caplog) -> None:
    io = _Workspace()
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
            await writer.append(_record("a"))

            with caplog.at_level(logging.WARNING), pytest.raises(TimeoutError) as raised:
                await writer.flush()
    finally:
        io.release.set()

    assert type(raised.value).__name__ == "WorkspaceWriteTimeout"
    assert io.started and len(io.started) == 1
    text = " | ".join(r.getMessage() for r in caplog.records)
    assert "s1" in text and "did not accept" in text, f"the loss was not logged: {text}"


async def test_a_broken_writer_starts_no_further_write_and_fails_at_once(bounded_writes) -> None:
    io = _Workspace()
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
            await writer.append(_record("a"))
            with pytest.raises(TimeoutError):
                await writer.flush()

            seq_before = writer.last_seq
            with pytest.raises(TimeoutError):
                await writer.append(_record("b"))
            with pytest.raises(TimeoutError):
                await writer.flush()
            with pytest.raises(TimeoutError):
                await writer.reserve_seq()
    finally:
        io.release.set()

    assert len(io.started) == 1, "a broken writer started another write"
    assert writer.last_seq == seq_before, "a record refused by a broken writer still took a seq"


async def test_the_abandoned_batch_lands_once_if_the_workspace_answers_late_and_what_was_buffered_behind_it_never_does(
    bounded_writes,
) -> None:
    io = _Workspace()
    async with asyncio.timeout(HARD_BOUND_S):
        writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
        await writer.append(_record("a"))
        first = asyncio.create_task(writer.flush())              # "a" is now in flight and never answers
        await _spin()
        assert len(io.started) == 1, "the flush did not reach the append: the test is not in its situation"
        await writer.append(_record("b"))                        # buffered behind it
        second = asyncio.create_task(writer.flush())

        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(o, TimeoutError) for o in outcomes), f"a flush did not give up: {outcomes!r}"

        io.release.set()                                         # the workspace answers, late
        await _spin()

        assert io.texts() == ["a"], f"the abandoned batch was lost, doubled, or something written behind it: {io.texts()}"
        assert len(io.started) == 1
        with pytest.raises(TimeoutError):
            await writer.flush()
        await _spin()
        assert io.texts() == ["a"], "a record buffered behind the abandoned batch was written after the workspace answered"


async def test_the_flush_an_append_triggers_gives_up_too(bounded_writes) -> None:
    """The age policy flushes inside ``append``: a turn streaming into a workspace that stopped answering is not held either."""
    io = _Workspace()
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
            await writer.append(_record("old"))
            await asyncio.sleep(0.15)                            # the buffered record is older than the 100 ms policy
            with pytest.raises(TimeoutError):
                await writer.append(_record("new"))
    finally:
        io.release.set()


async def test_a_flush_waiting_for_a_batch_another_flush_left_in_flight_gives_up_too(bounded_writes) -> None:
    """The batch was started by a flush that was cancelled meanwhile (a Stop's cancel of a call): the later flush waits for it so the
    file stays in order, and is bounded like the first."""
    io = _Workspace()
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
            await writer.append(_record("a"))
            cancelled = asyncio.create_task(writer.flush())
            await _spin()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled

            await writer.append(_record("b"))
            with pytest.raises(TimeoutError):
                await writer.flush()
    finally:
        io.release.set()


async def test_a_healthy_slow_write_is_not_abandoned(monkeypatch) -> None:
    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", 2.0, raising=False)
    io = _Workspace(answers_after=0.3)
    async with asyncio.timeout(HARD_BOUND_S):
        writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
        await writer.append(_record("a"))
        await writer.flush()
        await writer.append(_record("b"))
        await writer.flush()

    assert io.texts() == ["a", "b"]


async def test_a_write_that_fails_still_raises_as_itself_and_does_not_break_the_writer(bounded_writes) -> None:
    io = _Workspace(answers_after=0)
    io.fail_with = OSError("disk is gone")
    async with asyncio.timeout(HARD_BOUND_S):
        writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1")
        await writer.append(_record("a"))
        with pytest.raises(OSError, match="disk is gone"):
            await writer.flush()

        io.fail_with = None
        await writer.append(_record("b"))
        await writer.flush()

    assert io.texts() == ["b"], "a write that failed outright (not a hang) must not close the writer"


async def test_the_timeout_is_a_timeout_error() -> None:
    assert issubclass(persistence.WorkspaceWriteTimeout, TimeoutError)
