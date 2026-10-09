"""The timeline pairs a TOOL_RESULT with the call of its own RUN (ticket 01a11cd4).

``_tree`` (``primer/session/timeline.py``) kept the calls of a turn in one map keyed by ``(node_id, scoped call id)`` and paired a result with ``calls[(node_id, call_id)]``. The recorder
numbers a delegated run's scoped ids from a counter of its own (``x:tool:1:1`` for the run's first call), so a delegated call and its parent's, or the calls of two runs that overlap,
share a scoped id: the later call replaced the earlier in the map, and a result paired with whichever call was registered last. Run on the real delegation seed the PARENT's
``invoke_agent`` call ended with no status and no result, and the child's call showed the parent's answer. A delegated record carries ``delegate_run_id`` (results too), so the call is
looked up by the run as well.
"""

from __future__ import annotations

import json

from primer.session.timeline import build_turn_timeline
from tests.ui_e2e import _delegation_seed as seed

T0 = "2026-10-09T10:00:00+00:00"


def _rec(seq: int, kind: str, node_id: str | None = None, **payload) -> str:
    return json.dumps({"seq": seq, "kind": kind, "payload": payload, "created_at": T0, "node_id": node_id})


def _call(seq: int, scoped: str, raw: str, run: str | None = None, **extra) -> str:
    payload = {"id": scoped, "raw_id": raw, "name": "system__invoke_agent", "arguments": {}, **extra}
    if run is not None:
        payload.update({"delegated": True, "delegate_tool_call_id": extra.get("delegate_tool_call_id", "call_0"), "delegate_run_id": run})
    return _rec(seq, "tool_call", None, **payload)


def _result(seq: int, scoped: str, output: str, run: str | None = None, error: bool = False) -> str:
    payload = {"call_id": scoped, "output": output, "error": error}
    if run is not None:
        payload.update({"delegated": True, "delegate_tool_call_id": "call_0", "delegate_run_id": run})
    return _rec(seq, "tool_result", None, **payload)


def _timeline(lines: list[str]) -> dict:
    tl = build_turn_timeline(message_lines=[*lines, _rec(99, "done", None, stop_reason="stop")], turn_log_lines=[], turn_no=0)
    assert tl is not None
    return tl


def _walk(node: dict):
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


def _call_by_seq(tl: dict, seq: int) -> dict:
    return next(n for top in tl["children"] for n in _walk(top) if n["kind"] == "tool_call" and n["seq"] == seq)


def test_a_parents_call_and_its_childs_call_that_share_a_scoped_id_each_get_their_own_result() -> None:
    lines = [
        _call(1, "x:tool:1:1", "call_0"),
        _call(2, "x:tool:1:1", "call_0", run="r1"),
        _result(3, "x:tool:1:1", "the child's answer", run="r1"),
        _result(4, "x:tool:1:1", "helper finished"),
    ]
    tl = _timeline(lines)
    parent, child = _call_by_seq(tl, 1), _call_by_seq(tl, 2)
    assert parent["result"]["output"] == "helper finished" and parent["status"] == "ok"
    assert child["result"]["output"] == "the child's answer" and child["status"] == "ok"


def test_an_error_result_marks_only_the_call_of_its_own_run() -> None:
    lines = [
        _call(1, "x:tool:1:1", "call_0"),
        _call(2, "x:tool:1:1", "call_0", run="r1"),
        _result(3, "x:tool:1:1", "boom", run="r1", error=True),
        _result(4, "x:tool:1:1", "helper finished"),
    ]
    tl = _timeline(lines)
    assert _call_by_seq(tl, 1)["status"] == "ok"
    assert _call_by_seq(tl, 2)["status"] == "error"


def test_two_runs_that_overlap_and_number_their_calls_alike_keep_their_own_results() -> None:
    lines = [
        _call(1, "x:tool:1:1", "call_0"), _call(2, "x:tool:1:2", "call_1"),
        _call(3, "x:tool:1:1", "call_0", run="rA", delegate_tool_call_id="call_0"),
        _call(4, "x:tool:1:1", "call_9", run="rB", delegate_tool_call_id="call_1"),
        _result(5, "x:tool:1:1", "answer of rB", run="rB"),
        _result(6, "x:tool:1:1", "answer of rA", run="rA"),
    ]
    tl = _timeline(lines)
    assert _call_by_seq(tl, 3)["result"]["output"] == "answer of rA"
    assert _call_by_seq(tl, 4)["result"]["output"] == "answer of rB"


def test_the_real_delegation_seed_pairs_every_call_with_its_own_result() -> None:
    """The records the real recorder and translator write for a parent, a child and a grandchild (``tests/ui_e2e/_delegation_seed.py``)."""
    seeded = seed.build()
    tl = _timeline([json.dumps(r) for r in seeded.records if r["kind"] != "done" or r["payload"].get("delegate_run_id")])
    parent, child = _call_by_seq(tl, seeded.parent_call_seq), _call_by_seq(tl, seeded.child_call_seq)
    assert parent["status"] == "ok" and parent["result"]["output"] == "helper finished", parent
    assert child["status"] == "ok" and seed.GRANDCHILD in child["result"]["output"], child


def test_a_result_with_no_run_pairs_as_before_in_a_session_that_never_delegated() -> None:
    """The control: no delegated record anywhere, the map is keyed by the node and the id alone."""
    tl = _timeline([_call(1, "x:tool:1:1", "call_0"), _result(2, "x:tool:1:1", "fine")])
    assert _call_by_seq(tl, 1)["result"]["output"] == "fine"


def test_a_record_from_before_run_ids_still_pairs_by_node_and_scoped_id() -> None:
    """The control: delegated records written before ``delegate_run_id`` carry none, and pair exactly as they did."""
    lines = [_call(1, "x:tool:1:1", "call_0"), _result(2, "x:tool:1:1", "done")]
    assert _call_by_seq(_timeline(lines), 1)["status"] == "ok"
