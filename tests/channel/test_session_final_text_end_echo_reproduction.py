"""Regression for ticket 01a10b4e: a top-level End echo survives a preceding nested End.

``translate_stream_event`` deduplicates a sole End output against an ordinary assistant token when its output template is a
byte-identical pass-through. It retains the suppressed End until the stream proves there is another top-level End; then both
independent results are written in order. A nested pass-through End cannot erase that deferred parent result.

The records come from the real translator, as in ``test_session_final_text_graph_end.py``, and append the same graph-final
``done`` record the production writer adds after the translated stream ends.
"""

from __future__ import annotations

import json

from primer.channel.session_relay import derive_session_final_text
from primer.graph.base import _GraphEndOutputEvent
from primer.model.chat import Done, TextDelta
from primer.session.graph_end import graph_end_record
from primer.session.persistence import _CoalesceState, translate_stream_event

USER = {"kind": "user_input", "payload": {"text": "go"}}


def _as_dicts(rec) -> list[dict]:
    recs = rec if isinstance(rec, list) else ([rec] if rec is not None else [])
    return [json.loads(r.model_dump_json()) for r in recs]


def _node(state: _CoalesceState, node_id: str, text: str) -> list[dict]:
    translate_stream_event(TextDelta(text=text, index=0), state, node_id=node_id, turn_no=1)
    return _as_dicts(translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state, node_id=node_id, turn_no=1))


def _end(state: _CoalesceState, end_node_id: str, text: str, nested: bool = False) -> list[dict]:
    return _as_dicts(translate_stream_event(
        _GraphEndOutputEvent(text=text, parsed=None, end_node_id=end_node_id, nested=nested), state, turn_no=1,
    ))


def _graph_done() -> dict:
    return _as_dicts(graph_end_record(failed=False, ended_reason="completed"))[0]


def _a_graph_whose_first_end_echoes_the_subgraph() -> list[dict]:
    """``SUB -> e1`` echoes the subgraph's output, ``SUB -> e2`` transforms it; the subgraph's own End output (nested) is
    forwarded first, as the parent runs it."""
    state = _CoalesceState()
    return [
        USER, *_node(state, "worker", "raw"),
        *_end(state, "sub-exit", "Inner: raw", nested=True),
        *_end(state, "e1", "Inner: raw"),            # the echo
        *_end(state, "e2", "Other: Inner: raw"),     # the transformation
        _graph_done(),
    ]


def _two_top_level_ends_after_an_ordinary_echo(*, nested_echo: bool) -> tuple[list[dict], list[dict]]:
    """Return the full log and the records emitted when the second top-level End arrived."""
    state = _CoalesceState()
    records = [USER, *_node(state, "worker", "X")]
    if nested_echo:
        records.extend(_end(state, "sub-exit", "X", nested=True))
    records.extend(_end(state, "e1", "X"))
    second = _end(state, "e2", "Y")
    records.extend(second)
    records.append(_graph_done())
    return records, second


def test_scenario_nested_exit_and_both_top_level_ends_are_written():
    records = _a_graph_whose_first_end_echoes_the_subgraph()

    ends = [(r["payload"]["end_node_id"], r["payload"].get("nested")) for r in records if r["payload"].get("end_node_id")]
    assert ends == [("sub-exit", True), ("e1", None), ("e2", None)], (
        f"the translator wrote a different set of End records: {ends}"
    )


def test_every_top_level_end_output_is_in_the_final_text_even_when_one_echoes_a_subgraph():
    records = _a_graph_whose_first_end_echoes_the_subgraph()

    assert derive_session_final_text(records) == "Inner: raw\n\nOther: Inner: raw", (
        f"the final text lost the echoing End's output: {derive_session_final_text(records)!r}"
    )


def test_nested_passthrough_then_parent_echo_and_second_end_relays_both_top_level_results() -> None:
    records, second = _two_top_level_ends_after_an_ordinary_echo(nested_echo=True)

    assert [r["payload"]["end_node_id"] for r in second] == ["e1", "e2"], (
        "the second End must recover the deferred first End before itself"
    )
    assert records[-1]["payload"]["graph_end"] is True
    assert derive_session_final_text(records) == "X\n\nY"


def test_ordinary_echo_then_two_top_level_ends_relays_both_results() -> None:
    records, second = _two_top_level_ends_after_an_ordinary_echo(nested_echo=False)

    assert [r["payload"]["end_node_id"] for r in second] == ["e1", "e2"]
    assert records[-1]["payload"]["graph_end"] is True
    assert derive_session_final_text(records) == "X\n\nY"


def test_a_single_top_level_passthrough_stays_deduplicated_through_graph_done() -> None:
    state = _CoalesceState()
    records = [USER, *_node(state, "worker", "X")]

    assert _end(state, "e1", "X") == []
    records.append(_graph_done())

    assert derive_session_final_text(records) == "X"


def test_emitted_end_then_ordinary_text_then_same_text_end_relays_both_results() -> None:
    state = _CoalesceState()
    records = [
        USER,
        *_end(state, "e1", "A"),
        *_node(state, "worker", "X"),
    ]
    second = _end(state, "e2", "X")
    records.extend(second)
    records.append(_graph_done())

    assert [r["payload"]["end_node_id"] for r in second] == ["e2"]
    assert derive_session_final_text(records) == "A\n\nX"


def test_recovered_pair_then_ordinary_text_then_third_end_relays_all_results() -> None:
    state = _CoalesceState()
    records = [USER, *_node(state, "worker-1", "X")]
    assert _end(state, "e1", "X") == []
    recovered = _end(state, "e2", "Y")
    records.extend(recovered)
    records.extend(_node(state, "worker-2", "Z"))
    third = _end(state, "e3", "Z")
    records.extend(third)
    records.append(_graph_done())

    assert [r["payload"]["end_node_id"] for r in recovered] == ["e1", "e2"]
    assert [r["payload"]["end_node_id"] for r in third] == ["e3"]
    assert derive_session_final_text(records) == "X\n\nY\n\nZ"
