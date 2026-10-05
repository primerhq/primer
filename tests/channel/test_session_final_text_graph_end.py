"""The final text of a completed GRAPH run: the End node's output is the result.

A graph run writes each node's answer and its ``done`` as the nodes finish, and the End node then writes its OWN
``assistant_token`` record (``payload.end_node_id`` set) from its ``output_template``, AFTER the last node's ``done``.
That record is the graph's canonical output. A pass-through template writes no record at all (it would duplicate the
preceding answer), so the last node's answer stands in. The records here come from ``translate_stream_event``, the
translator production uses, so the shape cannot drift from what a real run writes.
"""

from __future__ import annotations

import json

from primer.channel.session_relay import derive_session_final_text
from primer.graph.base import _GraphEndOutputEvent
from primer.model.chat import Done, TextDelta
from primer.session.persistence import _CoalesceState, translate_stream_event

USER = {"kind": "user_input", "payload": {"text": "go"}}


def _as_dicts(rec) -> list[dict]:
    recs = rec if isinstance(rec, list) else ([rec] if rec is not None else [])
    return [json.loads(r.model_dump_json()) for r in recs]


def _node(state: _CoalesceState, node_id: str, text: str) -> list[dict]:
    """A node that streams ``text`` and finishes: its assistant_token and its done."""
    translate_stream_event(TextDelta(text=text, index=0), state, node_id=node_id, turn_no=1)
    return _as_dicts(translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state, node_id=node_id, turn_no=1))


def _end(state: _CoalesceState, end_node_id: str, text: str) -> list[dict]:
    return _as_dicts(translate_stream_event(
        _GraphEndOutputEvent(text=text, parsed=None, end_node_id=end_node_id), state, turn_no=1,
    ))


def test_a_transforming_end_template_is_the_result_not_the_last_nodes_raw_answer() -> None:
    state = _CoalesceState()
    records = [USER, *_node(state, "worker", "raw answer"), *_end(state, "end1", "Report: raw answer")]

    assert records[-1]["kind"] == "assistant_token" and records[-1]["payload"]["end_node_id"] == "end1", (
        "the End-output record is no longer written after the last done: this test would be vacuous"
    )
    assert derive_session_final_text(records) == "Report: raw answer"


def test_a_passthrough_end_writes_no_record_and_the_last_nodes_answer_is_the_result() -> None:
    state = _CoalesceState()
    records = [USER, *_node(state, "worker", "the answer"), *_end(state, "end1", "the answer")]

    assert [r["kind"] for r in records] == ["user_input", "assistant_token", "done"], "the duplicate was written"
    assert derive_session_final_text(records) == "the answer"


def test_an_end_with_an_empty_output_leaves_the_last_nodes_answer_as_the_result() -> None:
    state = _CoalesceState()
    records = [USER, *_node(state, "worker", "the answer"), *_end(state, "end1", "")]

    assert derive_session_final_text(records) == "the answer"


def test_two_end_outputs_are_both_in_the_result_in_order() -> None:
    state = _CoalesceState()
    records = [
        USER, *_node(state, "worker", "raw"), *_end(state, "endA", "First: raw"), *_end(state, "endB", "Second: raw"),
    ]

    assert derive_session_final_text(records) == "First: raw\n\nSecond: raw"


def test_streamed_text_after_the_end_output_is_still_an_unfinished_turn() -> None:
    """The trailing-text rule still holds for text that is NOT an End output (a node that streamed and never ended)."""
    state = _CoalesceState()
    records = [
        USER, *_node(state, "worker", "raw"), *_end(state, "end1", "Report: raw"),
        {"kind": "assistant_token", "payload": {"text": "a node still streaming"}, "node_id": "other"},
    ]

    assert derive_session_final_text(records) is None


def test_an_end_output_after_a_failed_node_is_not_a_result() -> None:
    state = _CoalesceState()
    translate_stream_event(TextDelta(text="half", index=0), state, node_id="worker", turn_no=1)
    failed = _as_dicts(translate_stream_event(Done(stop_reason="error", raw_reason="error"), state, node_id="worker", turn_no=1))
    records = [USER, *failed, *_end(state, "end1", "Report: half")]

    assert derive_session_final_text(records) is None


def test_an_end_output_followed_by_a_cancelled_record_is_not_a_result() -> None:
    state = _CoalesceState()
    records = [
        USER, *_node(state, "worker", "raw"), *_end(state, "end1", "Report: raw"),
        {"kind": "cancelled", "payload": {"reason": "operator_cancel"}},
    ]

    assert derive_session_final_text(records) is None
