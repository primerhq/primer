"""A graph turn is ONE window, however many nodes finished inside it (ticket 01a11f35, design: docs/superpowers/review-2026-10-08/graph-turn-windows-design.md).

``TurnWindowScanner`` ended a window at every ``done`` / ``cancelled`` / ``error`` of "the session's own run" and never read ``node_id``. In a graph session every node's stream writes its own
``done``, so ONE graph turn (a fan-out of two workers) was THREE windows: worker 0's end, worker 1's end, and the graph's own ``done`` written by dispatch after the executor's stream. The
timeline's windows, the trace's ordinals (and the join of window n to the n-th envelope run), ``session_usage.turns`` and the open-turn count all read that, so a running graph looked finished
the moment its first node was, a steer arriving mid-graph was written as a second user input, and every later turn of the session asked the trace for another turn's envelope.

The rule: a record with a ``node_id`` is INSIDE, as a delegated record is. The graph's own end is the first record without one: the run's final ``done``, dispatch's failure exit, the claim
adapter's release marker (a worker that died).

Every log below is written by the real writers: a real ``GraphExecutor`` fan-out through the real ``translate_stream_event`` and ``DelegationRecorder`` (``tests/session/graph_turn_shapes.py``).
"""

from __future__ import annotations

import json

import pytest

from primer.session.terminals import CLOSES, COPY, INSIDE, TurnWindowScanner
from primer.session.timeline import build_turn_timeline, turn_envelopes, turn_windows
from primer.session.turns import count_turn_state, has_open_turn
from primer.session.usage import session_usage
from tests.session.graph_turn_shapes import FailingWorkerLLM, cancelled, graph_turn, release_marker, with_park

# name -> (the arguments of graph_turn as a factory, the status the turn ends in)
SHAPES = {
    "success": (lambda: {}, "completed"),
    "both-nodes-fail": (lambda: {"llm": FailingWorkerLLM(), "finish": "failed"}, "failed"),
    "one-node-fails-and-the-run-halts": (lambda: {"llm": FailingWorkerLLM((2,)), "finish": "failed"}, "failed"),
    "one-node-fails-and-the-fan-out-continues": (lambda: {"llm": FailingWorkerLLM((2,)), "on_failure": "collect"}, "completed"),
    "nested-one-level": (lambda: {"depth": 1}, "completed"),
    "nested-two-levels": (lambda: {"depth": 2}, "completed"),
}


@pytest.fixture(params=sorted(SHAPES))
async def shape(request, monkeypatch):
    make, status = SHAPES[request.param]
    return request.param, await graph_turn(monkeypatch=monkeypatch, **make()), status


def _lines(records: list[dict]) -> list[str]:
    return [json.dumps(r) for r in records]


def _own_end(records: list[dict]) -> int:
    """The seq of the graph's own end: the first terminal-looking record that is neither a node's nor a delegated run's."""
    return next(
        r["seq"] for r in records
        if r["kind"] in ("done", "error", "cancelled") and not r.get("node_id") and not (r.get("payload") or {}).get("delegated")
    )


def test_a_graph_turn_is_one_window_closed_by_its_own_end(shape):
    name, records, _ = shape

    windows = turn_windows(_lines(records))

    assert len(windows) == 1, f"{name}: {[(w['turn_no'], w['terminal_seq']) for w in windows]}"
    assert [r["seq"] for r in windows[0]["records"]] == [r["seq"] for r in records]
    assert windows[0]["terminal_seq"] == _own_end(records)


def test_the_counts_see_one_turn(shape):
    name, records, status = shape
    lines = _lines(records)

    state = count_turn_state(lines, cursor=0)

    assert session_usage(lines).turns == 1, name
    assert (state.open_user_inputs, state.terminals, state.open_turns) == (1, 1, 0), name
    assert has_open_turn(lines, cursor=0) is False
    timeline = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert timeline is not None and timeline["status"] == status, name
    assert build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=1) is None, f"{name}: the graph turn must not have a second window"


def test_node_records_are_inside_and_only_the_graphs_own_end_closes(shape):
    name, records, _ = shape
    scanner = TurnWindowScanner()

    verdicts = [(rec, scanner.feed(rec)) for rec in records]

    for rec, verdict in verdicts:
        if rec.get("node_id") or (rec.get("payload") or {}).get("delegated"):
            assert verdict == INSIDE, f"{name}: seq {rec['seq']} {rec['kind']} of node {rec.get('node_id')} must be inside"
    closing = [rec["seq"] for rec, verdict in verdicts if verdict == CLOSES]
    assert closing == [_own_end(records)], name
    # whatever follows the end is a copy of it (dispatch's failure exit then the release marker), never a second end
    assert all(verdict in (INSIDE, COPY) for rec, verdict in verdicts if rec["seq"] > closing[0]), name


def test_every_call_row_and_its_result_are_filed_in_one_window(shape):
    """The #659 symptom: worker 1's ``tool_result`` landed in the window AFTER the one holding its call, because worker 0's final ``done`` sat between them."""
    name, records, _ = shape
    window_of = {rec["seq"]: window["turn_no"] for window in turn_windows(_lines(records)) for rec in window["records"]}

    node_tool_rows = [r["seq"] for r in records if r["kind"] in ("tool_call", "tool_result") and r.get("node_id")]

    assert node_tool_rows, f"premise: {name} holds node tool rows"
    assert {window_of[seq] for seq in node_tool_rows} == {0}, name


@pytest.mark.asyncio
async def test_a_running_graph_is_an_open_turn_whatever_its_nodes_have_finished(monkeypatch):
    """Cut at every record before the graph's own end: the first node's ``done`` used to read as 'no open turn' (so a steer was written as a SECOND user input and a crashed
    graph's input was not re-armed)."""
    records = await graph_turn(monkeypatch=monkeypatch)
    end = _own_end(records)

    for cut in range(1, end):
        lines = _lines([r for r in records if r["seq"] <= cut])
        state = count_turn_state(lines, cursor=0)
        assert (state.terminals, state.open_turns) == (0, 1), f"cut at {cut}: {records[cut - 1]['kind']} of {records[cut - 1].get('node_id')}"
        assert has_open_turn(lines, cursor=0) is True, cut
        assert session_usage(lines).turns == 0, cut
        windows = turn_windows(lines)
        assert [w["terminal_seq"] for w in windows] == [None], cut


@pytest.mark.asyncio
async def test_a_graph_whose_worker_died_after_its_nodes_finished_is_closed_by_the_release_marker(monkeypatch):
    """No final ``done`` was written (the lease was lost after the nodes' last ``done``): the claim adapter's marker, the first record without a node, ends the turn."""
    records = await graph_turn(monkeypatch=monkeypatch)
    last_node_done = max(r["seq"] for r in records if r["kind"] == "done" and r.get("node_id") and not (r["payload"] or {}).get("delegated"))
    died = [r for r in records if r["seq"] <= last_node_done]
    marker = release_marker().model_copy(update={"seq": last_node_done + 1}).model_dump(mode="json")
    lines = _lines(died + [marker])

    windows = turn_windows(lines)
    state = count_turn_state(lines, cursor=0)

    assert [w["terminal_seq"] for w in windows] == [marker["seq"]]
    assert (state.terminals, state.open_turns) == (1, 0)
    assert session_usage(lines).turns == 1


@pytest.mark.asyncio
async def test_the_second_turn_of_a_graph_session_gets_its_own_records_and_envelope(monkeypatch):
    """The trace joins window n to the n-th envelope run. Three windows per graph turn made window 1 the FIRST turn's second node and left the second turn without a window of its own."""
    first = await graph_turn(monkeypatch=monkeypatch)
    second = await graph_turn(monkeypatch=monkeypatch)
    shifted = [dict(r, seq=r["seq"] + len(first)) for r in second]
    lines = _lines(first + shifted)
    turn_log = [
        json.dumps({"seq": 1, "kind": "started", "turn_no": 0, "ts": "2026-10-09T12:00:01+00:00"}),
        json.dumps({"seq": 2, "kind": "completed", "turn_no": 0, "ts": "2026-10-09T12:00:40+00:00"}),
        json.dumps({"seq": 3, "kind": "started", "turn_no": 1, "ts": "2026-10-09T12:05:01+00:00"}),
        json.dumps({"seq": 4, "kind": "completed", "turn_no": 1, "ts": "2026-10-09T12:05:44+00:00"}),
    ]

    assert [w["terminal_seq"] for w in turn_windows(lines)] == [_own_end(first), len(first) + _own_end(second)]
    assert len(turn_envelopes(turn_log)) == 2
    turn_one = build_turn_timeline(message_lines=lines, turn_log_lines=turn_log, turn_no=1)
    assert turn_one is not None
    assert (turn_one["started_at"], turn_one["ended_at"]) == ("2026-10-09T12:05:01+00:00", "2026-10-09T12:05:44+00:00")
    assert turn_one["status"] == "completed"
    assert [w["records"][0]["seq"] for w in turn_windows(lines)] == [1, len(first) + 1]       # each window starts at its own user input
    assert build_turn_timeline(message_lines=lines, turn_log_lines=turn_log, turn_no=2) is None
    assert session_usage(lines).turns == 2


@pytest.mark.asyncio
async def test_a_log_from_before_records_carried_a_node_id_keeps_its_old_windows(monkeypatch):
    """Nothing in such a log says which node wrote a record, so it cannot be told from a non-graph log: it stays over-windowed, as it was (the rule's stated limit)."""
    records = await graph_turn(monkeypatch=monkeypatch)
    stripped = [{k: v for k, v in r.items() if k != "node_id"} for r in records]

    assert len(turn_windows(_lines(stripped))) == 3


def _first_node_done(records: list[dict]) -> int:
    """The seq of the first node ``done`` that ends a node (not a tool round's ``done(tool_use)``)."""
    return next(
        r["seq"] for r in records
        if r["kind"] == "done" and r.get("node_id") and not (r["payload"] or {}).get("delegated") and r["payload"].get("stop_reason") != "tool_use"
    )


@pytest.mark.asyncio
async def test_a_graph_stopped_midway_is_one_window_closed_by_the_cancel(monkeypatch):
    """A Stop writes dispatch's ``cancelled`` (no node). The nodes that had finished before it are inside the window it closes."""
    records = await graph_turn(monkeypatch=monkeypatch)
    cut = _first_node_done(records)
    stop = cancelled().model_copy(update={"seq": cut + 1}).model_dump(mode="json")
    lines = _lines([r for r in records if r["seq"] <= cut] + [stop])

    windows = turn_windows(lines)
    state = count_turn_state(lines, cursor=0)

    assert [w["terminal_seq"] for w in windows] == [stop["seq"]]
    assert (state.terminals, state.open_turns) == (1, 0)
    timeline = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert timeline is not None and timeline["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_stop_that_lands_after_the_graph_finished_is_a_second_window_as_for_any_turn(monkeypatch):
    """Unchanged by the rule: a ``done`` followed by the ``cancelled`` of a Stop that arrived after the run finished counts as two terminals (``session_usage`` documents it)."""
    records = await graph_turn(monkeypatch=monkeypatch)
    stop = cancelled().model_copy(update={"seq": len(records) + 1}).model_dump(mode="json")

    windows = turn_windows(_lines(records + [stop]))

    assert [w["terminal_seq"] for w in windows] == [_own_end(records), stop["seq"]]


@pytest.mark.asyncio
async def test_a_graph_that_parks_and_resumes_is_still_one_window(monkeypatch):
    """A park writes ``yielded`` and, when the answer arrives, ``resumed``: neither ends anything, and while it waits the turn is open."""
    records = await graph_turn(monkeypatch=monkeypatch)
    parked = with_park(records, after_seq=_first_node_done(records))
    park_seq = next(r["seq"] for r in parked if r["kind"] == "yielded")

    lines = _lines(parked)
    waiting = _lines([r for r in parked if r["seq"] <= park_seq])

    assert [w["terminal_seq"] for w in turn_windows(lines)] == [parked[-1]["seq"]]
    assert session_usage(lines).turns == 1
    assert has_open_turn(waiting, cursor=0) is True
    assert [w["terminal_seq"] for w in turn_windows(waiting)] == [None]
