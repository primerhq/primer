"""The tap never serves a legacy ERROR record's traceback.

ERROR records written before the error envelope stopped carrying a
traceback still hold ``payload.extensions.traceback`` in messages.jsonl.
The tap parses that file itself (``read_session_since`` for the SSE tap,
``read_batch`` for the MCP ``workspace_tap`` drain, which any user may
call), so the strip has to live where every reader of ERROR records
passes: the shared record parse and the TapEvent builder.
"""

from __future__ import annotations

import json

import pytest

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.tap.cursor import TapCursor
from primer.tap.event import record_to_tap_event
from primer.tap.reader import read_batch, read_record_by_seq, read_session_since
from primer.tap.selector import TapSelector
from tests.tap.test_mcp_tap_tool import _build, _WID
from tests.tap.test_reader import (
    _NOW,
    _FakeWorkspaceIO,
    _Provider,
    _make_standalone_session,
    _msg_path,
    _seed_session,
)

_TB = (
    "Traceback (most recent call last):\n"
    '  File "/app/primer/session/dispatch.py", line 1, in run\n'
    "ValueError: boom\n"
)


def _legacy_error_line(seq: int) -> bytes:
    rec = {
        "seq": seq,
        "kind": "error",
        "payload": {
            "message": "boom", "code": "/errors/internal",
            "title": "ValueError", "status": 500,
            "extensions": {"exception_class": "ValueError", "traceback": _TB},
        },
        "created_at": _NOW.isoformat(),
    }
    return (json.dumps(rec) + "\n").encode()


def _assert_clean(payload: dict) -> None:
    assert "traceback" not in payload["extensions"]
    assert payload["extensions"]["exception_class"] == "ValueError"
    assert payload["message"] == "boom"
    assert "dispatch.py" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_sse_tap_read_from_an_old_cursor_strips_the_traceback() -> None:
    io = _FakeWorkspaceIO()
    sess = await _make_standalone_session("s1")
    io.write(_msg_path("s1"), _legacy_error_line(1))
    events, _ = await read_session_since(
        io, workspace_id="ws-1", session=sess, after_seq=0,
        selector=TapSelector(),
    )
    [ev] = events
    _assert_clean(ev.payload)


@pytest.mark.asyncio
async def test_read_batch_drain_from_seq_zero_strips_the_traceback() -> None:
    provider = _Provider()
    io = _FakeWorkspaceIO()
    await _seed_session(provider.store, "s1")
    io.write(_msg_path("s1"), _legacy_error_line(1))
    events, _ = await read_batch(
        provider, io, workspace_id="ws-1", selector=TapSelector(),
        cursor=TapCursor(seqs={"s1": 0}, known_as_of=_NOW), limit=100,
    )
    [ev] = events
    _assert_clean(ev.payload)


@pytest.mark.asyncio
async def test_read_record_by_seq_strips_the_traceback() -> None:
    io = _FakeWorkspaceIO()
    io.write(_msg_path("s1"), _legacy_error_line(4))
    record = await read_record_by_seq(io, session_id="s1", seq=4)
    assert record is not None
    _assert_clean(record.payload)


@pytest.mark.asyncio
async def test_mcp_workspace_tap_drain_tool_strips_the_traceback() -> None:
    """The user-callable MCP drain: no traceback in its tool output."""
    provider = _Provider()
    io = _FakeWorkspaceIO()
    await _seed_session(provider.store, "s1")
    io.write(_msg_path("s1"), _legacy_error_line(1))
    ts = _build(provider, io)
    result = await ts.call(
        tool_name="workspace_tap", arguments={"workspace_id": _WID},
    )
    assert not result.is_error, result.output
    assert "Traceback" not in result.output
    assert "dispatch.py" not in result.output
    [ev] = json.loads(result.output)["events"]
    _assert_clean(ev["payload"])


def test_the_tap_event_builder_strips_a_traceback_from_any_record() -> None:
    """A record that reaches the builder by another path (not parsed by
    the reader) is cleaned too, without mutating the record."""
    record = SessionMessageRecord(
        seq=1, kind=SessionMessageKind.ERROR, created_at=_NOW,
        payload={
            "message": "boom",
            "extensions": {"exception_class": "ValueError", "traceback": _TB},
        },
    )
    ev = record_to_tap_event(
        record, workspace_id="ws-1", session_id="s1", agent_id="ag1",
        graph_id=None, cursor="s1:1",
    )
    assert "traceback" not in ev.payload["extensions"]
    assert ev.payload["extensions"]["exception_class"] == "ValueError"
