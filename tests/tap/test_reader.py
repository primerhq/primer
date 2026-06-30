"""Tests for primer.tap.reader — TapReader incremental log engine.

Two read paths are exercised:

* :func:`read_session_since` — the single-session *incremental* fast-path used
  by the live SSE tick loop. It reads from a byte offset, parses only complete
  newline-terminated lines, and returns the offset after the last complete
  line so the caller never re-scans.
* :func:`read_batch` — the multi-session *drain* used by the MCP surface. It
  resolves in-scope sessions via the selector, reads each from the start by
  ``seq``, maps + filters, accumulates up to ``limit``, and advances the cursor
  per session.

A small in-memory fake workspace IO (mirroring the real workspace ``read_file``)
serves seeded ``messages.jsonl`` bytes and counts reads so the incremental
behaviour can be asserted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.tap.cursor import TapCursor
from primer.tap.event import TapEventClass
from primer.tap.reader import read_batch, read_session_since
from primer.tap.selector import TapSelector

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeWorkspaceIO:
    """In-memory workspace IO exposing ``read_file`` + ``state_path``.

    ``read_calls`` counts every ``read_file`` invocation and ``bytes_read``
    accumulates the total bytes returned, so tests can prove the incremental
    path does not re-scan the whole file.
    """

    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self.read_calls: int = 0
        self.bytes_read: int = 0

    def write(self, path: str, content: bytes) -> None:
        self._files[path] = content

    def append(self, path: str, content: bytes) -> None:
        self._files[path] = self._files.get(path, b"") + content

    async def read_file(self, path: str) -> bytes:
        self.read_calls += 1
        if path not in self._files:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        data = self._files[path]
        self.bytes_read += len(data)
        return data


def _msg_path(session_id: str) -> str:
    return f".state/sessions/{session_id}/messages.jsonl"


def _line(seq: int, kind: str, **payload) -> bytes:
    rec = {
        "seq": seq,
        "kind": kind,
        "payload": payload,
        "created_at": _NOW.isoformat(),
    }
    return (json.dumps(rec) + "\n").encode()


async def _seed_session(
    store, sid: str, *, agent_id: str | None = "ag1", graph_id: str | None = None,
    status: SessionStatus = SessionStatus.RUNNING,
) -> WorkspaceSession:
    if graph_id is not None:
        binding = GraphSessionBinding(graph_id=graph_id)
    else:
        binding = AgentSessionBinding(agent_id=agent_id or "ag1")
    sess = WorkspaceSession(
        id=sid,
        workspace_id="ws-1",
        binding=binding,
        status=status,
        created_at=_NOW,
        turn_status="idle",
    )
    await store.create(sess)
    return sess


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


# ---------------------------------------------------------------------------
# read_session_since — incremental single-session path
# ---------------------------------------------------------------------------


class TestReadSessionSince:
    @pytest.mark.asyncio
    async def test_reads_records_after_seq(self) -> None:
        io = _FakeWorkspaceIO()
        sess = await _make_standalone_session("s1")
        io.write(
            _msg_path("s1"),
            _line(1, "user_input") + _line(2, "tool_call") + _line(3, "done"),
        )
        events, offset = await read_session_since(
            io,
            workspace_id="ws-1",
            session=sess,
            after_seq=1,
            selector=TapSelector(),
        )
        assert [e.payload for e in events] == [{}, {}]
        assert [e.class_ for e in events] == [
            TapEventClass.TOOL_CALL,
            TapEventClass.DONE,
        ]
        # workspace/session/agent injected
        assert events[0].workspace_id == "ws-1"
        assert events[0].session_id == "s1"
        assert events[0].agent_id == "ag1"
        assert events[0].graph_id is None
        assert offset == len(io._files[_msg_path("s1")])

    @pytest.mark.asyncio
    async def test_missing_file_returns_offset_unchanged(self) -> None:
        io = _FakeWorkspaceIO()  # nothing written
        sess = await _make_standalone_session("s-missing")
        events, offset = await read_session_since(
            io,
            workspace_id="ws-1",
            session=sess,
            after_seq=0,
            selector=TapSelector(),
            from_offset=7,
        )
        assert events == []
        assert offset == 7

    @pytest.mark.asyncio
    async def test_incremental_does_not_rescan(self) -> None:
        io = _FakeWorkspaceIO()
        sess = await _make_standalone_session("s2")
        first = _line(1, "user_input") + _line(2, "tool_call")
        io.write(_msg_path("s2"), first)

        events1, offset1 = await read_session_since(
            io, workspace_id="ws-1", session=sess, after_seq=0,
            selector=TapSelector(),
        )
        assert [e.class_ for e in events1] == [
            TapEventClass.USER_INPUT,
            TapEventClass.TOOL_CALL,
        ]
        assert offset1 == len(first)

        # New record appended; second call resumes from offset1.
        second = _line(3, "done")
        io.append(_msg_path("s2"), second)

        bytes_before = io.bytes_read
        events2, offset2 = await read_session_since(
            io, workspace_id="ws-1", session=sess, after_seq=0,
            selector=TapSelector(), from_offset=offset1,
        )
        # Only the NEW record comes back — seq 1/2 are not re-emitted.
        assert [e.class_ for e in events2] == [TapEventClass.DONE]
        assert offset2 == len(first) + len(second)
        # The second read consumed only the new bytes, not the whole file.
        assert io.bytes_read - bytes_before <= len(io._files[_msg_path("s2")])
        assert offset2 > offset1

    @pytest.mark.asyncio
    async def test_partial_trailing_line_not_emitted(self) -> None:
        io = _FakeWorkspaceIO()
        sess = await _make_standalone_session("s3")
        full = _line(1, "user_input")
        partial = b'{"seq":2,"kind":"done","payload":{},'  # no closing + no \n
        io.write(_msg_path("s3"), full + partial)

        events, offset = await read_session_since(
            io, workspace_id="ws-1", session=sess, after_seq=0,
            selector=TapSelector(),
        )
        # Only the complete record is emitted; the partial is left for later.
        assert [e.class_ for e in events] == [TapEventClass.USER_INPUT]
        assert offset == len(full)  # points BEFORE the partial line

        # Now the rest of the partial line lands (closing it + newline).
        rest = b'"created_at":"' + _NOW.isoformat().encode() + b'"}\n'
        # Replace the file with the now-complete content.
        io.write(_msg_path("s3"), full + partial + rest)
        events2, offset2 = await read_session_since(
            io, workspace_id="ws-1", session=sess, after_seq=0,
            selector=TapSelector(), from_offset=offset,
        )
        assert [e.class_ for e in events2] == [TapEventClass.DONE]
        assert offset2 == len(io._files[_msg_path("s3")])

    @pytest.mark.asyncio
    async def test_event_selector_filters(self) -> None:
        io = _FakeWorkspaceIO()
        sess = await _make_standalone_session("s4")
        io.write(
            _msg_path("s4"),
            _line(1, "user_input") + _line(2, "tool_call") + _line(3, "tool_result"),
        )
        sel = TapSelector(
            events=Predicate(
                left=FieldRef(name="class"),
                op=Op.EQ,
                right=Value(value="tool_call"),
            )
        )
        events, _ = await read_session_since(
            io, workspace_id="ws-1", session=sess, after_seq=0, selector=sel,
        )
        assert [e.class_ for e in events] == [TapEventClass.TOOL_CALL]


# ---------------------------------------------------------------------------
# read_batch — multi-session drain
# ---------------------------------------------------------------------------


class TestReadBatch:
    @pytest.mark.asyncio
    async def test_across_two_sessions_after_cursor(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1", agent_id="ag1")
        await _seed_session(provider.store, "s2", agent_id="ag2")
        io.write(
            _msg_path("s1"),
            _line(1, "user_input") + _line(2, "tool_call") + _line(3, "done"),
        )
        io.write(
            _msg_path("s2"),
            _line(1, "user_input") + _line(2, "tool_result"),
        )
        cursor = TapCursor(seqs={"s1": 1, "s2": 0}, known_as_of=_NOW)

        events, new_cursor = await read_batch(
            provider,
            io,
            workspace_id="ws-1",
            selector=TapSelector(),
            cursor=cursor,
            limit=100,
        )
        # s1: seq>1 → 2,3 ; s2: seq>0 → 1,2 ; stable order by session then seq.
        by_session = {}
        for e in events:
            by_session.setdefault(e.session_id, []).append(e.class_)
        assert by_session["s1"] == [TapEventClass.TOOL_CALL, TapEventClass.DONE]
        assert by_session["s2"] == [
            TapEventClass.USER_INPUT,
            TapEventClass.TOOL_RESULT,
        ]
        # Cursor advanced per session to the highest consumed seq.
        assert new_cursor.resume_seq("s1") == 3
        assert new_cursor.resume_seq("s2") == 2
        # agent_id injected from the binding per session.
        assert {e.session_id: e.agent_id for e in events} == {
            "s1": "ag1",
            "s2": "ag2",
        }

    @pytest.mark.asyncio
    async def test_applies_event_selector(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1", agent_id="ag1")
        io.write(
            _msg_path("s1"),
            _line(1, "user_input") + _line(2, "tool_call") + _line(3, "done"),
        )
        sel = TapSelector(
            events=Predicate(
                left=FieldRef(name="class"),
                op=Op.IN,
                right=Value(value=["tool_call", "done"]),
            )
        )
        events, _ = await read_batch(
            provider, io, workspace_id="ws-1", selector=sel,
            cursor=TapCursor(seqs={}, known_as_of=_NOW), limit=100,
        )
        assert [e.class_ for e in events] == [
            TapEventClass.TOOL_CALL,
            TapEventClass.DONE,
        ]

    @pytest.mark.asyncio
    async def test_respects_limit(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1", agent_id="ag1")
        io.write(
            _msg_path("s1"),
            _line(1, "user_input") + _line(2, "tool_call")
            + _line(3, "tool_result") + _line(4, "done"),
        )
        events, new_cursor = await read_batch(
            provider, io, workspace_id="ws-1", selector=TapSelector(),
            cursor=TapCursor(seqs={}, known_as_of=_NOW), limit=2,
        )
        assert len(events) == 2
        assert [e.class_ for e in events] == [
            TapEventClass.USER_INPUT,
            TapEventClass.TOOL_CALL,
        ]
        # Cursor advanced only as far as consumed.
        assert new_cursor.resume_seq("s1") == 2

    @pytest.mark.asyncio
    async def test_session_excluded_by_selector_yields_nothing(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        # Two sessions; selector keeps only s-keep via binding.agent_id.
        await _seed_session(provider.store, "s-keep", agent_id="ag-keep")
        await _seed_session(provider.store, "s-drop", agent_id="ag-drop")
        io.write(_msg_path("s-keep"), _line(1, "done"))
        io.write(_msg_path("s-drop"), _line(1, "done"))

        sel = TapSelector(
            sessions=Predicate(
                left=FieldRef(name="binding.agent_id"),
                op=Op.EQ,
                right=Value(value="ag-keep"),
            )
        )
        events, new_cursor = await read_batch(
            provider, io, workspace_id="ws-1", selector=sel,
            cursor=TapCursor(seqs={}, known_as_of=_NOW), limit=100,
        )
        assert [e.session_id for e in events] == ["s-keep"]
        assert new_cursor.resume_seq("s-drop") == 0

    @pytest.mark.asyncio
    async def test_missing_log_file_is_empty(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()  # no messages.jsonl for the session
        await _seed_session(provider.store, "s1", agent_id="ag1")
        events, new_cursor = await read_batch(
            provider, io, workspace_id="ws-1", selector=TapSelector(),
            cursor=TapCursor(seqs={}, known_as_of=_NOW), limit=100,
        )
        assert events == []
        assert new_cursor.resume_seq("s1") == 0


# ---------------------------------------------------------------------------
# helpers that need no store
# ---------------------------------------------------------------------------


async def _make_standalone_session(sid: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_NOW,
        turn_status="idle",
    )
