"""Review round 1 (I2-3): the MCP drain's token is bounded.

A watermark (``TapCursor.drained_as_of``) moves only when a WORKSPACE-WIDE
drain (no ``sessions`` predicate) read every session to the end (the
``limit`` was not cut into): a scoped drain's scope is a subset of the
workspace, and a watermark set on it would let a later, broader drain skip
the sessions the scoped one never read (review 2026-11-23, I2 r2).  A
complete drain drops the ENDED sessions it fully drained from the token,
and ``read_batch`` skips a session that is absent from the cursor, ENDED,
and ended at or before the watermark (ENDED is terminal; a reopen
``ENDED -> CREATED`` clears ``status`` and ``ended_at``, so a reopened
session is read again, from the start).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.tap.cursor import TapCursor
from primer.tap.reader import read_batch
from primer.tap.selector import TapSelector
from tests._support.segmented_io import ListsFiles

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)


# ---------------------------------------------------------------------------
# Fakes (same shape as tests/tap/test_reader.py)
# ---------------------------------------------------------------------------


class _FakeWorkspaceIO(ListsFiles):
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self.read_calls = 0

    def write(self, path: str, content: bytes) -> None:
        self._files[path] = content

    def append(self, path: str, content: bytes) -> None:
        self._files[path] = self._files.get(path, b"") + content

    async def read_file(self, path: str) -> bytes:
        self.read_calls += 1
        if path not in self._files:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        return self._files[path]


def _msg_path(session_id: str) -> str:
    return f".state/sessions/{session_id}/messages.jsonl"


def _line(seq: int, kind: str = "done", at: datetime | None = None) -> bytes:
    rec = {"seq": seq, "kind": kind, "payload": {}, "created_at": (at or T0).isoformat()}
    return (json.dumps(rec) + "\n").encode()


class _Provider:
    """Minimal storage provider exposing ``get_storage`` over one store."""

    def __init__(self) -> None:
        from tests.conftest import _InMemoryStorage

        self._store = _InMemoryStorage(WorkspaceSession)

    def get_storage(self, model_cls):  # noqa: ANN001
        return self._store

    @property
    def store(self):
        return self._store


async def _seed(store, sid: str, *, status: SessionStatus = SessionStatus.RUNNING, ended_at: datetime | None = None) -> None:
    await store.create(
        WorkspaceSession(
            id=sid,
            workspace_id="ws-1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=status,
            created_at=T0,
            turn_status="idle",
            ended_at=ended_at,
        )
    )


# ---------------------------------------------------------------------------
# The watermark: set only by a drain that read every in-scope session to the end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_over_many_ended_sessions_stays_bounded():
    """The agent pays the token in context on every call: 100 ended sessions must not make it 100 entries."""
    provider = _Provider()
    io = _FakeWorkspaceIO()
    n = 100
    for i in range(n):
        sid = f"s{i:03d}"
        await _seed(provider.store, sid, status=SessionStatus.ENDED, ended_at=T0)
        io.write(_msg_path(sid), b"".join(_line(s) for s in range(1, 6)))

    events, cursor = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor(seqs={}, known_as_of=T0), limit=1000, drained_at=T1,
    )
    assert len(events) == n * 5
    # A complete drain moves the watermark and drops every ENDED session it drained.
    assert cursor.drained_as_of == T1
    assert cursor.seqs == {} and cursor.offsets == {}
    token = cursor.encode()
    assert len(token) < 300, "the token names no session"

    # The next drain skips the ended sessions without reading their logs and returns nothing.
    reads_before = io.read_calls
    events2, cursor2 = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor.decode(token), limit=1000, drained_at=T2,
    )
    assert events2 == []
    assert io.read_calls == reads_before, "a skipped session is not read at all"
    assert cursor2.drained_as_of == T2
    assert cursor2.seqs == {} and len(cursor2.encode()) < 300


@pytest.mark.asyncio
async def test_an_incomplete_drain_moves_no_watermark_and_loses_nothing():
    """The limit cut session ``a`` short and never visited ``b``: no watermark, nothing dropped, nothing lost or duped."""
    provider = _Provider()
    io = _FakeWorkspaceIO()
    await _seed(provider.store, "a", status=SessionStatus.RUNNING)
    await _seed(provider.store, "b", status=SessionStatus.ENDED, ended_at=T0)
    io.write(_msg_path("a"), b"".join(_line(s) for s in range(1, 11)))
    io.write(_msg_path("b"), b"".join(_line(s) for s in range(1, 6)))

    cursor = TapCursor(seqs={}, known_as_of=T0)
    events1, cursor = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=3, drained_at=T1,
    )
    assert [(e.session_id, e.seq) for e in events1] == [("a", 1), ("a", 2), ("a", 3)]
    assert cursor.drained_as_of == EPOCH, "the limit cut the drain: the watermark stays put"

    events2, cursor = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=100, drained_at=T2,
    )
    assert [(e.session_id, e.seq) for e in events2] == (
        [("a", s) for s in range(4, 11)] + [("b", s) for s in range(1, 6)]
    )
    assert cursor.drained_as_of == T2
    assert "a" in cursor.seqs, "the still-active session stays named in the token"
    assert "b" not in cursor.seqs, "the drained ended session leaves the token"

    events3, _ = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=100,
        drained_at=T2 + timedelta(minutes=1),
    )
    assert events3 == []


# ---------------------------------------------------------------------------
# The skip: a missing session that ended at or before the watermark
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_skip_requires_an_end_at_or_before_the_watermark():
    provider = _Provider()
    io = _FakeWorkspaceIO()
    late_end = T1 + timedelta(seconds=1)
    await _seed(provider.store, "late", status=SessionStatus.ENDED, ended_at=late_end)
    io.write(_msg_path("late"), _line(1, at=late_end))
    await _seed(provider.store, "early", status=SessionStatus.ENDED, ended_at=T0)
    io.write(_msg_path("early"), _line(1))

    cursor = TapCursor(seqs={}, known_as_of=T0, drained_as_of=T1)
    events, _ = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=100,
    )
    assert [e.session_id for e in events] == ["late"], (
        "``early`` ended before the watermark (its log was drained by the drain that set it) and is skipped; "
        "``late`` ended after it and is read"
    )


@pytest.mark.asyncio
async def test_a_reopened_ended_session_is_read_again():
    """The drain drops ``s1``; a reopen (``ENDED -> CREATED``, ``ended_at`` cleared) and new records: it is read again."""
    provider = _Provider()
    io = _FakeWorkspaceIO()
    await _seed(provider.store, "s1", status=SessionStatus.ENDED, ended_at=T0)
    io.write(_msg_path("s1"), b"".join(_line(s) for s in range(1, 6)))

    _, cursor = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor(seqs={}, known_as_of=T0), limit=100, drained_at=T1,
    )
    assert "s1" not in cursor.seqs, "a fully drained ENDED session leaves the token"

    # wake_session's reopen: ENDED -> CREATED, ended_at and ended_reason cleared.
    row = await provider.store.get("s1")
    row.status = SessionStatus.CREATED
    row.ended_at = None
    row.ended_reason = None
    io.append(_msg_path("s1"), _line(6, at=T2) + _line(7, at=T2))

    events, _ = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(), cursor=cursor, limit=100, drained_at=T2,
    )
    # The entry was dropped, so it is read from the start: the new records arrive (with the old ones re-offered).
    assert [(e.session_id, e.seq) for e in events] == [("s1", s) for s in range(1, 8)]


# ---------------------------------------------------------------------------
# The token field itself
# ---------------------------------------------------------------------------


def test_the_watermark_round_trips_and_old_tokens_default_to_the_epoch():
    cursor = TapCursor(seqs={"a": 1}, known_as_of=T0, drained_as_of=T1)
    assert TapCursor.decode(cursor.encode()).drained_as_of == T1

    raw = json.dumps({"seqs": {"a": 1}, "known_as_of": T0.isoformat(), "offsets": {}}).encode()
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert TapCursor.decode(token).drained_as_of == EPOCH, "a pre-watermark token skips nothing"


# ---------------------------------------------------------------------------
# The watermark is workspace-wide: a scoped drain never moves it (I2 r2)
# ---------------------------------------------------------------------------


def _pin(session_id: str) -> TapSelector:
    """A ``sessions`` predicate that confines the drain to one session id."""
    return TapSelector(sessions=Predicate(left=FieldRef(name="id"), op=Op.EQ, right=Value(value=session_id)))


@pytest.mark.asyncio
async def test_a_scoped_drain_moves_no_watermark_and_a_broader_drain_reads_what_it_missed():
    """A complete drain under a NARROW selector must not move the watermark: a later, broader drain on the same
    token chain has to read the ENDED sessions the scoped drain never read (review 2026-11-23, I2 r2)."""
    provider = _Provider()
    io = _FakeWorkspaceIO()
    await _seed(provider.store, "narrow", status=SessionStatus.ENDED, ended_at=T0)
    io.write(_msg_path("narrow"), b"".join(_line(s) for s in range(1, 6)))
    await _seed(provider.store, "wide", status=SessionStatus.ENDED, ended_at=T0)
    io.write(_msg_path("wide"), b"".join(_line(s) for s in range(1, 6)))

    # Drain 1: complete under the narrow selector. Its scope never saw "wide", so its
    # completeness must not become a watermark a broader scope can skip against.
    events1, cursor = await read_batch(
        provider, io, workspace_id="ws-1", selector=_pin("narrow"),
        cursor=TapCursor(seqs={}, known_as_of=T0), limit=100, drained_at=T1,
    )
    assert [(e.session_id, e.seq) for e in events1] == [("narrow", s) for s in range(1, 6)]
    assert cursor.drained_as_of == EPOCH, "a scoped drain never moves the watermark"
    assert "narrow" not in cursor.seqs, "a scoped complete drain still drops the ENDED sessions it drained"

    # Drain 2: the same token chain, the scope broadened to the whole workspace.
    # "wide" was never read and must not be skipped as if it had been.
    token = cursor.encode()
    events2, cursor2 = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor.decode(token), limit=100, drained_at=T2,
    )
    got2 = {(e.session_id, e.seq) for e in events2}
    assert {("wide", s) for s in range(1, 6)} <= got2, "the never-read ENDED session is read, not skipped"
    # This workspace-wide drain IS complete, so it now moves the watermark and drops both.
    assert cursor2.drained_as_of == T2
    assert cursor2.seqs == {} and cursor2.offsets == {}

    # Drain 3: the skip is legitimate now (both logs were read to the end).
    reads_before = io.read_calls
    events3, _ = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor.decode(cursor2.encode()), limit=100,
        drained_at=T2 + timedelta(minutes=1),
    )
    assert events3 == [] and io.read_calls == reads_before


@pytest.mark.asyncio
async def test_a_reopened_session_that_ends_again_is_read_again():
    """The drop/reopen/re-end lifecycle against the token's OWN watermark: the second ``ended_at`` is after the
    watermark of the drain that dropped the session, so the skip must not fire and the log is re-read from the
    start (review 2026-11-23, I2 r1 lead question, pinned)."""
    provider = _Provider()
    io = _FakeWorkspaceIO()
    await _seed(provider.store, "s1", status=SessionStatus.ENDED, ended_at=T0)
    io.write(_msg_path("s1"), b"".join(_line(s) for s in range(1, 6)))

    # Drain 1: complete -> s1 fully drained, dropped from the token, watermark T1.
    _, cursor = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor(seqs={}, known_as_of=T0), limit=100, drained_at=T1,
    )
    token = cursor.encode()
    assert "s1" not in cursor.seqs and cursor.drained_as_of == T1

    # Reopen (wake_session): ENDED -> CREATED, ended_at cleared; new records; ends again at T2 > T1.
    row = await provider.store.get("s1")
    row.status = SessionStatus.CREATED
    row.ended_at = None
    row.ended_reason = None
    io.append(_msg_path("s1"), _line(6, at=T2) + _line(7, at=T2))
    row.status = SessionStatus.ENDED
    row.ended_at = T2

    reads_before = io.read_calls
    events2, cursor2 = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor.decode(token), limit=100, drained_at=T2 + timedelta(minutes=1),
    )
    assert [(e.session_id, e.seq) for e in events2] == [("s1", s) for s in range(1, 8)], (
        "the second ended_at (T2) is after the token's watermark (T1): the skip must not fire"
    )
    assert io.read_calls > reads_before, "the reopened session's log is re-read, not skipped"
    assert cursor2.drained_as_of == T2 + timedelta(minutes=1)

    # Drain 3: the skip is legitimate now (drain 2 read the log to the end).
    reads_before3 = io.read_calls
    events3, _ = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor.decode(cursor2.encode()), limit=100,
        drained_at=T2 + timedelta(minutes=2),
    )
    assert events3 == [] and io.read_calls == reads_before3
