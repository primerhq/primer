"""A failed turn is ONE window, whatever the number of records it wrote (ticket 01a11ca5).

One failed user turn leaves up to four terminal-looking records: the stream's own (an ``error``, or ``done`` with ``stop_reason: "error"``
for OpenResponses), dispatch's failure exit ERROR, the claim adapter's release marker, and, for a graph, one ERROR per failing node.
``closes_turn`` counted every one of them, so the timeline's windows, the trace's ordinals, ``session_usage.turns`` and the open-turn count saw
a failed turn as two or three turns. The rule now (``terminals.TurnWindowScanner``): the FIRST error of a failure ends the window, and a later
error of the same turn with the same words from the same node, or a bare release marker, is a COPY of it: it ends nothing and is filed with the
window it copies. Folded on read: old logs read the same way, and nothing is written differently.

The shapes below are the ones the writers produce (read from ``persistence.py``, ``dispatch._end_turn_failed`` and
``claim/adapters/sessions._write_terminal_record``); the last tests drive those writers for real.
"""

from __future__ import annotations

import json

import pytest

from primer.session.timeline import build_turn_timeline, turn_windows
from primer.session.turns import count_turn_state
from primer.session.usage import session_usage
from tests.session.test_trace_envelopes_after_a_failed_turn import (  # noqa: F401  (fixtures are used by name)
    _play,
    _times,
    fake_event_bus,
    fake_storage_provider,
)

M = "The upstream answered 500"


def _rec(seq: int, kind: str, node_id: str | None = None, **payload) -> str:
    return json.dumps({"seq": seq, "kind": kind, "node_id": node_id, "payload": payload, "created_at": "2026-10-09T00:00:00+00:00"})


def _stream_error(seq, message=M, node_id=None, fatal=True):
    return _rec(seq, "error", node_id, message=message, code="server_error", fatal=fatal)


def _dispatch_error(seq, message=M, node_id=None):
    return _rec(seq, "error", node_id, message=message, code="/errors/internal", title="Internal error", status=500)


def _marker(seq):
    return _rec(seq, "error", reason="unknown", terminal=True)


def _user(seq):
    return _rec(seq, "user_input", text="go")


def _done(seq, stop_reason="stop", **extra):
    return _rec(seq, "done", stop_reason=stop_reason, **extra)


# (name, records, the seqs that END a window, the seqs of each window's records)
SHAPES = [
    (
        "a fatal adapter error: the stream's error, dispatch's copy of it, the release marker",
        [_user(1), _stream_error(2), _dispatch_error(3), _marker(4)],
        [2], [[1, 2, 3, 4]],
    ),
    (
        "OpenResponses response.failed: the done(error) ends it, the held non-fatal error and dispatch's error and the marker are copies",
        [_user(1), _done(2, "error"), _stream_error(3, fatal=False), _dispatch_error(4), _marker(5)],
        [2], [[1, 2, 3, 4, 5]],
    ),
    (
        "OpenResponses error with no response.failed: the non-fatal error is a notice, dispatch's error is the end of the turn",
        [_user(1), _stream_error(2, fatal=False), _dispatch_error(3), _marker(4)],
        [3], [[1, 2, 3, 4]],
    ),
    (
        "a failure with no stream error (a tool or an invariant): dispatch's error ends it, the marker is a copy",
        [_user(1), _dispatch_error(2), _marker(3)],
        [2], [[1, 2, 3]],
    ),
    (
        "a worker crash: the release marker is the only evidence and ends the turn",
        [_user(1), _marker(2)],
        [2], [[1, 2]],
    ),
    (
        "a graph run with two failing nodes keeps two: different node, different words",
        [_user(1), _stream_error(2, "node a failed", "a"), _stream_error(3, "node b failed", "b"), _marker(4)],
        [2, 3], [[1, 2], [3, 4]],
    ),
    (
        "the same words from two NAMED nodes are two failures",
        [_user(1), _stream_error(2, node_id="a"), _stream_error(3, node_id="b")],
        [2, 3], [[1, 2], [3]],
    ),
    (
        "the same words from a record that names no node are a copy of any node's failure",
        [_user(1), _stream_error(2, node_id="a"), _dispatch_error(3)],
        [2], [[1, 2, 3]],
    ),
    (
        "streamed text between a failure and its copy: the copy still belongs to the failed turn",
        [_user(1), _stream_error(2), _rec(3, "assistant_token", text="hi"), _dispatch_error(4), _marker(5)],
        [2], [[1, 2, 3, 4, 5]],
    ),
    (
        "a retry that fails the same way is a SECOND failed turn: a user message ends the first one's copies",
        [_user(1), _stream_error(2), _dispatch_error(3), _marker(4), _user(5), _stream_error(6), _dispatch_error(7), _marker(8)],
        [2, 6], [[1, 2, 3, 4], [5, 6, 7, 8]],
    ),
    (
        "a failure after a successful turn with no user message between (an autonomous run) is a new failure, even with the same words",
        [_user(1), _stream_error(2), _dispatch_error(3), _user(4), _done(5), _stream_error(6)],
        [2, 5, 6], [[1, 2, 3], [4, 5], [6]],
    ),
    (
        "a subagent's error is the subagent's, never the session's, and never the cause the parent's copy folds into",
        [_user(1), _rec(2, "error", None, message=M, delegated=True, delegate_run_id="r1"), _stream_error(3), _dispatch_error(4)],
        [3], [[1, 2, 3, 4]],
    ),
    (
        "a retry notice the turn recovered from ends nothing",
        [_user(1), _stream_error(2, fatal=False), _done(3)],
        [3], [[1, 2, 3]],
    ),
    (
        "a cancelled turn is one window",
        [_user(1), _rec(2, "cancelled", reason="user"), _user(3), _done(4)],
        [2, 4], [[1, 2], [3, 4]],
    ),
    (
        "a failed turn, then a good one: the failed turn's copies stay with it, not with the next turn",
        [_user(1), _stream_error(2), _dispatch_error(3), _marker(4), _rec(5, "invocation_divider"), _user(6), _done(7)],
        [2, 7], [[1, 2, 3, 4], [5, 6, 7]],
    ),
]
IDS = [shape[0][:60] for shape in SHAPES]


@pytest.mark.parametrize("name, lines, ends, window_seqs", SHAPES, ids=IDS)
def test_the_timeline_windows_end_once_per_failure(name, lines, ends, window_seqs):
    windows = turn_windows(lines)

    assert [w["terminal_seq"] for w in windows] == ends
    assert [[r["seq"] for r in w["records"]] for w in windows] == window_seqs


@pytest.mark.parametrize("name, lines, ends, window_seqs", SHAPES, ids=IDS)
def test_the_session_usage_turns_count_once_per_failure(name, lines, ends, window_seqs):
    assert session_usage(lines).turns == len(ends)


@pytest.mark.parametrize("name, lines, ends, window_seqs", SHAPES, ids=IDS)
def test_the_open_turn_count_counts_once_per_failure(name, lines, ends, window_seqs):
    assert count_turn_state(lines, cursor=0).terminals == len(ends)


def test_the_last_record_of_a_failed_turn_is_still_a_terminal_for_its_status():
    """``is_session_terminal`` stays true for a copy at the END of the log, or a failed turn whose last record is the marker would read as running."""
    lines = [_user(1), _stream_error(2), _dispatch_error(3), _marker(4)]

    timeline = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)

    assert timeline is not None and timeline["status"] == "failed"


@pytest.mark.asyncio
async def test_a_failed_turn_written_by_the_production_writers_is_one_window(fake_storage_provider, fake_event_bus):
    message_lines, _, _ = await _play(fake_storage_provider, fake_event_bus, ["fail"])

    kinds = [json.loads(line)["kind"] for line in message_lines if '"seq"' in line]
    assert kinds.count("error") >= 2, f"premise: the writers leave more than one error record: {kinds}"
    assert [w["terminal_seq"] is not None for w in turn_windows(message_lines)] == [True]
    assert session_usage(message_lines).turns == 1


@pytest.mark.asyncio
async def test_fail_retry_fail_serves_each_turn_its_own_envelope(fake_storage_provider, fake_event_bus):
    """The trace joins window n to the n-th envelope run by position: with the failed turns one window each the join is right (it was off by the
    extra windows, see 01a11ce4's interim test, which this replaces)."""
    message_lines, turn_log_lines, _ = await _play(fake_storage_provider, fake_event_bus, ["fail", "ok", "fail"])

    timelines = []
    for n in range(8):
        timeline = build_turn_timeline(message_lines=message_lines, turn_log_lines=turn_log_lines, turn_no=n)
        if timeline is None:
            break
        timelines.append(timeline)

    assert [t["status"] for t in timelines] == ["failed", "completed", "failed"]
    from primer.session.timeline import turn_envelopes

    envelopes = turn_envelopes(turn_log_lines)
    assert [(t["started_at"], t["ended_at"]) for t in timelines] == [_times(group) for group in envelopes]
