"""Reproduction of ticket 01a10b4e: a top-level End output that echoes a subgraph's output is dropped from the final text.

TEST-ONLY: no production change. ``translate_stream_event`` suppresses an End output as a duplicate when its coalesced text
equals ``state.last_assistant_token``, the text of the record written just before it, and that check does not tell a NESTED
record (a subgraph's End output forwarded by its parent) from a top-level one. ``derive_session_final_text`` falls back to the
last nested output only when NO top-level End output exists. So a graph with ``SUB -> e1`` (template ``{{ nodes.SUB.text }}``,
an echo) and ``SUB -> e2`` (template ``Other: {{ nodes.SUB.text }}``) loses e1: its record is suppressed against the preceding
nested record, e2 is a top-level output so the fallback does not fire, and the result is e2's text alone. That breaks the
documented rule that several top-level End outputs are joined (``test_two_end_outputs_are_both_in_the_result_in_order``).

The records come from the real translator, as in ``test_session_final_text_graph_end.py``. A scenario test pins what the
translator wrote (so a changed harness cannot hide behind the expected failure); the behaviour test is a strict xfail
restricted to ``AssertionError``. The fix (do not suppress a top-level End output against a nested record, or count the
suppressed echo in ``derive_session_final_text``) must delete the marker.
"""

from __future__ import annotations

import json

import pytest

from primer.channel.session_relay import derive_session_final_text
from primer.graph.base import _GraphEndOutputEvent
from primer.model.chat import Done, TextDelta
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


def _a_graph_whose_first_end_echoes_the_subgraph() -> list[dict]:
    """``SUB -> e1`` echoes the subgraph's output, ``SUB -> e2`` transforms it; the subgraph's own End output (nested) is
    forwarded first, as the parent runs it."""
    state = _CoalesceState()
    return [
        USER, *_node(state, "worker", "raw"),
        *_end(state, "sub-exit", "Inner: raw", nested=True),
        *_end(state, "e1", "Inner: raw"),            # the echo
        *_end(state, "e2", "Other: Inner: raw"),     # the transformation
    ]


def test_scenario_the_echoing_end_wrote_no_record_and_the_transforming_one_did():
    records = _a_graph_whose_first_end_echoes_the_subgraph()

    ends = [(r["payload"]["end_node_id"], r["payload"].get("nested")) for r in records if r["payload"].get("end_node_id")]
    assert ends == [("sub-exit", True), ("e2", None)], f"the translator wrote a different set of End records: {ends}"


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="01a10b4e: the dedup against last_assistant_token ignores nestedness, so e1's output is dropped from the result",
)
def test_every_top_level_end_output_is_in_the_final_text_even_when_one_echoes_a_subgraph():
    records = _a_graph_whose_first_end_echoes_the_subgraph()

    assert derive_session_final_text(records) == "Inner: raw\n\nOther: Inner: raw", (
        f"the final text lost the echoing End's output: {derive_session_final_text(records)!r}"
    )
