"""The graph's own end: the record, its predicate, and every reader that has to treat it as the verdict of a graph turn (ticket 01a11f35, round 2 of #701).

HAND-BUILT records, for the unit cases the real-writer pins (``test_graph_turn_real_writers.py``) cannot reach cheaply: the shape of the record and of ``is_graph_end``, the window scanner's
rule (the end closes the window, and is a copy of a failure that already closed it), ``session_usage`` (the end is a turn end and not a model call), the timeline's status (a ``done`` that
says ``stop_reason: "error"`` is a failed turn) and the final-result relay (the end is the verdict, not a text boundary).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from primer.channel.session_relay import _parse_tail, derive_session_final_text
from primer.session.graph_end import GRAPH_ENDED, GRAPH_FAILED, graph_end_for, graph_end_record
from primer.session.terminals import CLOSES, COPY, INSIDE, TurnWindowScanner, is_graph_end
from primer.session.timeline import build_turn_timeline, turn_windows
from primer.session.usage import session_usage


def rec(seq: int, kind: str, payload: dict | None = None, node: str | None = None) -> dict:
    return {"seq": seq, "kind": kind, "payload": payload or {}, "node_id": node, "created_at": "2026-10-10T00:00:00Z"}


def seqd(records: list[dict]) -> list[dict]:
    return [dict(r, seq=i + 1) for i, r in enumerate(records)]


def lines(records: list[dict]) -> list[str]:
    return [json.dumps(r) for r in records]


def end(failed: bool = False) -> dict:
    return graph_end_record(failed=failed, ended_reason="failed" if failed else "completed").model_dump(mode="json")


USER = rec(0, "user_input", {"text": "go"})
DIVIDER = rec(0, "invocation_divider", {"invocation": 2})


def node_done(node: str, **usage) -> dict:
    payload = {"stop_reason": "stop", "raw_reason": "stop"}
    if usage:
        payload["usage"] = usage
    return rec(0, "done", payload, node)


def tok(text: str, node: str | None = None, **extra) -> dict:
    return rec(0, "assistant_token", {"text": text, **extra}, node)


def end_output(text: str, **extra) -> dict:
    return rec(0, "assistant_token", {"text": text, "end_node_id": "end", **extra}, "end")


# ---- the record ------------------------------------------------------------------------------------------------------------------------------------------


def test_the_end_of_a_graph_that_completed() -> None:
    record = graph_end_record(failed=False, ended_reason="completed")

    assert record.kind.value == "done" and record.node_id is None
    assert record.payload == {"stop_reason": "stop", "raw_reason": GRAPH_ENDED, "graph_end": True, "ended_reason": "completed"}


def test_the_end_of_a_graph_that_failed_is_a_failure_end() -> None:
    record = graph_end_record(failed=True, ended_reason="failed")

    assert record.kind.value == "done" and record.node_id is None
    assert (record.payload["stop_reason"], record.payload["raw_reason"], record.payload["graph_end"]) == ("error", GRAPH_FAILED, True)


def test_the_ended_reason_is_optional() -> None:
    assert "ended_reason" not in graph_end_record(failed=False).payload


@pytest.mark.parametrize("reason", [None, "stop", "end_turn", "error", "max_tokens", "tool_turn_cap", ""])
def test_an_agent_turn_has_no_graph_end(reason) -> None:
    assert graph_end_for(reason, SimpleNamespace()) is None


@pytest.mark.parametrize(("reason", "failed"), [("graph_ended", False), ("graph_failed", True)])
def test_a_graph_runs_end_names_how_the_executor_ended(reason: str, failed: bool) -> None:
    executor = SimpleNamespace(_last_ended_reason="max_iterations" if failed else "completed")

    record = graph_end_for(reason, executor)

    assert record is not None and record.payload["ended_reason"] == executor._last_ended_reason
    assert (record.payload["stop_reason"] == "error") is failed


def test_the_executor_inside_a_turn_driver_is_read_too() -> None:
    driver = SimpleNamespace(_executor=SimpleNamespace(_last_ended_reason="routing_failed"))

    assert graph_end_for("graph_failed", driver).payload["ended_reason"] == "routing_failed"


def test_an_executor_with_no_readable_reason_still_ends_the_graph() -> None:
    record = graph_end_for("graph_ended", SimpleNamespace(_last_ended_reason=object()))

    assert record is not None and "ended_reason" not in record.payload


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        pytest.param(end(), True, id="the end"),
        pytest.param(end(failed=True), True, id="the failure end"),
        pytest.param(rec(1, "done", {"stop_reason": "stop", "graph_end": True}, node="w0"), False, id="with a node"),
        pytest.param(rec(1, "done", {"stop_reason": "stop", "graph_end": True, "delegated": True}), False, id="delegated"),
        pytest.param(rec(1, "error", {"graph_end": True}), False, id="an error is not the end"),
        pytest.param(rec(1, "done", {"stop_reason": "stop"}), False, id="an ordinary done"),
        pytest.param(rec(1, "done", {"stop_reason": "stop", "graph_end": "true"}), False, id="a flag that is not the boolean"),
        pytest.param(rec(1, "done", {"stop_reason": "stop", "graph_end": 1}), False, id="a flag that is a number"),
        pytest.param({"seq": 1, "kind": "done", "payload": "text"}, False, id="a payload that is not an object"),
    ],
)
def test_is_graph_end(record: dict, expected: bool) -> None:
    assert is_graph_end(record) is expected


# ---- the window scanner ----------------------------------------------------------------------------------------------------------------------------------


def _verdicts(records: list[dict]) -> list[str]:
    scanner = TurnWindowScanner()
    return [scanner.feed(r) for r in records]


def test_the_end_closes_the_window_of_a_graph_whose_nodes_finished() -> None:
    assert _verdicts([USER, node_done("w0"), node_done("w1"), end()]) == [INSIDE, INSIDE, INSIDE, CLOSES]


def test_a_graph_level_error_that_names_no_node_has_already_closed_the_window_and_the_end_is_a_copy() -> None:
    """A top-level ``max_iterations_exceeded`` or routing error names no node: it ends the window, and the failure end that follows is that failure's copy, not a second window."""
    records = seqd([USER, node_done("w0"), rec(0, "error", {"code": "max_iterations_exceeded", "message": "graph ran for 1 iterations"}), end(failed=True)])

    assert _verdicts(records) == [INSIDE, INSIDE, CLOSES, COPY]
    windows = turn_windows(lines(records))
    assert len(windows) == 1 and windows[0]["terminal_seq"] == 3 and [r["seq"] for r in windows[0]["records"]] == [1, 2, 3, 4]


def test_the_release_marker_after_a_failure_end_is_a_copy_of_it() -> None:
    marker = rec(0, "error", {"reason": "unknown", "terminal": True})

    assert _verdicts([USER, node_done("w0"), end(failed=True), marker]) == [INSIDE, INSIDE, CLOSES, COPY]


def test_a_node_level_error_does_not_make_the_turn_failed_so_the_end_still_closes() -> None:
    """A node's error is inside the window and sets nothing: the failure end that follows is the window's one terminal, not a copy of it."""
    node_error = rec(0, "error", {"code": "server_error", "message": "boom", "fatal": True}, node="w0")

    assert _verdicts([USER, node_error, end(failed=True)]) == [INSIDE, INSIDE, CLOSES]


def test_a_reopened_invocation_has_its_own_end() -> None:
    first = [USER, node_done("w0"), end(failed=True)]
    second = [DIVIDER, USER, node_done("w0"), end()]

    assert _verdicts(first + second) == [INSIDE, INSIDE, CLOSES, INSIDE, INSIDE, INSIDE, CLOSES], "the user input of the reopen forgets the failure: the second end is a window of its own"
    assert len(turn_windows(lines(seqd(first + second)))) == 2


# ---- usage ------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_end_is_a_turn_and_not_a_model_call() -> None:
    records = seqd([
        USER,
        node_done("w0", input_tokens=10, output_tokens=2),
        node_done("w1", input_tokens=20, output_tokens=3),
        end(),
    ])

    usage = session_usage(lines(records))

    assert (usage.turns, usage.model_calls) == (1, 2)
    assert (usage.total_input_tokens, usage.total_output_tokens, usage.last_input_tokens) == (30, 5, 20), "the end carries no usage and does not touch the totals or the last call"


def test_a_graph_that_failed_is_still_one_turn() -> None:
    assert session_usage(lines(seqd([USER, node_done("w0"), end(failed=True)]))).turns == 1


# ---- the timeline's status --------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("records", "status"),
    [
        pytest.param([USER, node_done("w0"), end()], "completed", id="a graph that completed"),
        pytest.param([USER, node_done("w0"), end(failed=True)], "failed", id="a graph that failed"),
        pytest.param([USER, rec(0, "done", {"stop_reason": "error"})], "failed", id="an agent turn whose last record is a done that says error"),
        pytest.param([USER, rec(0, "done", {"stop_reason": "stop"})], "completed", id="an agent turn that stopped"),
        pytest.param([USER, node_done("w0")], "running", id="a graph that has not ended"),
    ],
)
def test_the_status_of_a_window_with_no_turn_log(records: list[dict], status: str) -> None:
    timeline = build_turn_timeline(message_lines=lines(seqd(records)), turn_log_lines=[], turn_no=0)

    assert timeline is not None and timeline["status"] == status


# ---- the final-result relay ---------------------------------------------------------------------------------------------------------------------------


def _final(records: list[dict]) -> str | None:
    return derive_session_final_text(seqd(records))


def test_the_end_nodes_output_is_the_result() -> None:
    assert _final([USER, tok("a", "w0"), node_done("w0"), tok("b", "w1"), node_done("w1"), end_output("A+B"), end()]) == "A+B"


def test_a_pass_through_end_relays_the_last_nodes_answer() -> None:
    assert _final([USER, tok("the answer", "w0"), node_done("w0"), end()]) == "the answer"


def test_a_graph_whose_nodes_wrote_no_done_relays_the_end_nodes_output() -> None:
    assert _final([USER, end_output("hello"), end()]) == "hello"


def test_the_text_of_an_earlier_invocation_is_not_relayed_with_the_next_ones() -> None:
    first = [USER, tok("old", "w0"), node_done("w0"), end_output("OLD END"), end()]
    second = [DIVIDER, USER, tok("new", "w0"), node_done("w0"), end()]

    assert _final(first + second) == "new"


def test_only_the_top_level_end_output_is_the_result() -> None:
    assert _final([USER, tok("x", "w0"), node_done("w0"), end_output("inner", nested=True), end_output("outer"), end()]) == "outer"


def test_the_last_nested_output_stands_in_when_no_top_level_one_was_written() -> None:
    assert _final([USER, tok("x", "w0"), node_done("w0"), end_output("inner", nested=True), end()]) == "inner"


def test_a_failed_graph_relays_nothing() -> None:
    assert _final([USER, tok("half", "w0"), node_done("w0"), end_output("partial"), end(failed=True)]) is None


def test_text_that_never_ended_after_the_last_node_is_not_a_result() -> None:
    assert _final([USER, tok("a", "w0"), node_done("w0"), tok("stray", "w1"), end()]) is None


def test_an_end_alone_has_no_text_to_relay() -> None:
    assert _final([USER, end()]) is None


def test_a_cancel_after_the_end_is_not_a_completed_turn() -> None:
    assert _final([USER, tok("a", "w0"), node_done("w0"), end_output("A"), end(), rec(0, "cancelled", {"reason": "user"})]) is None


# ---- the tail the relay parses ------------------------------------------------------------------------------------------------------------------------


def test_the_tail_the_relay_parses_reaches_the_node_before_the_last_one() -> None:
    """Parsing stops after two TEXT boundaries; the graph's end is the verdict and is not one, so the window of the last node is still in the tail."""
    records = seqd([USER, tok("first", "w0"), node_done("w0"), tok("second", "w1"), node_done("w1"), tok("third", "w2"), node_done("w2"), end()])
    reversed_lines = list(reversed(lines(records)))

    tail = _parse_tail(iter(reversed_lines))

    assert [r["seq"] for r in tail] == [5, 6, 7, 8]            # from the second-to-last node's done: its window's text (6) and the last node's done (7) and the end (8)
    assert derive_session_final_text(tail) == "third"


def test_a_log_that_does_not_end_in_a_graph_end_is_parsed_as_before() -> None:
    records = seqd([USER, tok("a", "w0"), node_done("w0"), tok("b", "w1"), node_done("w1"), tok("c", "w2"), node_done("w2")])

    tail = _parse_tail(iter(reversed(lines(records))))

    assert [r["seq"] for r in tail] == [5, 6, 7]


# ---- round 3: the invocation_divider (hand-built records; the real writers are in test_graph_turn_reopen_paths.py) ---------------------------------------------------


AGENT_DONE = rec(0, "done", {"stop_reason": "stop", "raw_reason": "stop"})          # an agent turn's own end: no node
AGENT_MARKER = rec(0, "agent_marker", {"agent_id": "a"})
RELEASE_MARKER = rec(0, "error", {"reason": "unknown", "terminal": True})
STREAM_ERROR = rec(0, "error", {"message": "boom", "code": "server_error", "fatal": True})
DISPATCH_FAILURE = rec(0, "error", {"message": "boom", "code": "/errors/internal", "title": "Internal error", "status": 500})


def test_a_divider_after_a_closed_agent_turn_is_the_first_record_of_the_next_window() -> None:
    assert _verdicts([USER, AGENT_DONE, DIVIDER, USER, AGENT_DONE]) == [INSIDE, CLOSES, INSIDE, INSIDE, CLOSES]
    assert len(turn_windows(lines(seqd([USER, AGENT_DONE, DIVIDER, USER, AGENT_DONE])))) == 2


def test_a_divider_after_a_failure_and_its_copies_is_inside_and_forgets_the_failure() -> None:
    records = [USER, STREAM_ERROR, DISPATCH_FAILURE, RELEASE_MARKER, DIVIDER, USER, STREAM_ERROR, DISPATCH_FAILURE, RELEASE_MARKER]

    assert _verdicts(records) == [INSIDE, CLOSES, COPY, COPY, INSIDE, INSIDE, CLOSES, COPY, COPY]
    assert len(turn_windows(lines(seqd(records)))) == 2


def test_a_divider_after_an_agent_marker_is_inside() -> None:
    assert _verdicts([USER, AGENT_DONE, AGENT_MARKER, DIVIDER, USER, AGENT_DONE]) == [INSIDE, CLOSES, INSIDE, INSIDE, INSIDE, CLOSES]


def test_an_agent_input_with_no_node_record_before_a_divider_stays_in_the_next_window() -> None:
    """No node record means no graph run is open: the divider closes nothing, exactly as before this rule."""
    records = seqd([USER, DIVIDER, USER, AGENT_DONE])

    assert _verdicts(records) == [INSIDE, INSIDE, INSIDE, CLOSES]
    assert len(turn_windows(lines(records))) == 1


def test_a_delegated_record_does_not_open_a_graph_run() -> None:
    delegated = rec(0, "done", {"stop_reason": "stop", "delegated": True})

    assert _verdicts([USER, delegated, DIVIDER, USER, AGENT_DONE]) == [INSIDE, INSIDE, INSIDE, INSIDE, CLOSES]


def test_a_divider_closes_a_graph_run_that_wrote_no_end_and_is_its_last_record() -> None:
    records = seqd([USER, node_done("w0"), DIVIDER, USER, node_done("w0"), end()])

    assert _verdicts(records) == [INSIDE, INSIDE, CLOSES, INSIDE, INSIDE, CLOSES]
    windows = turn_windows(lines(records))
    assert [(w["terminal_seq"], [r["seq"] for r in w["records"]]) for w in windows] == [(3, [1, 2, 3]), (6, [4, 5, 6])]


def test_a_divider_after_a_failed_graph_end_is_inside_and_the_next_end_is_not_a_copy() -> None:
    """The review's B1 as a unit case: ``[user, node done, end(error), divider, node done, end]``. The restart wrote no user_input, so only the divider can forget the failure."""
    records = [USER, node_done("w0"), end(failed=True), DIVIDER, node_done("w0"), end()]

    assert _verdicts(records) == [INSIDE, INSIDE, CLOSES, INSIDE, INSIDE, CLOSES]


def test_a_node_record_opens_a_run_and_every_close_ends_it() -> None:
    graph_error = rec(0, "error", {"code": "max_iterations_exceeded", "message": "graph ran for 1 iterations"})

    assert _verdicts([USER, node_done("w0"), graph_error, DIVIDER, USER, AGENT_DONE]) == [INSIDE, INSIDE, CLOSES, INSIDE, INSIDE, CLOSES]
    assert _verdicts([USER, node_done("w0"), end(), DIVIDER, USER, node_done("w0"), DIVIDER]) == [INSIDE, INSIDE, CLOSES, INSIDE, INSIDE, INSIDE, CLOSES]


def test_the_open_user_input_of_a_run_the_divider_closed_is_counted_as_closed() -> None:
    from primer.session.turns import count_turn_state

    state = count_turn_state(lines(seqd([USER, node_done("w0"), DIVIDER, USER, node_done("w0"), end()])), cursor=0)

    assert (state.open_user_inputs, state.terminals, state.open_turns) == (2, 2, 0)
