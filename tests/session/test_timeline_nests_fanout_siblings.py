"""The session timeline nests the runs of concurrent graph siblings under their OWN node's call (ticket 01a11cca, the timeline half).

``_attach`` (``primer/session/timeline.py``) looked a delegated record up by ``(delegate_parent_run_id, delegate_tool_call_id)``: the delegating call's RAW provider id, last call
winning. Two fan-out siblings (graph nodes ``A`` and ``B``) whose providers synthesise ``call_0`` both delegate under it at the same time, so both runs nested under the call written
last. The recorder stamps ``delegate_node_id`` on a run's records now (the node whose agent made the call, the ``node_id`` the parent's call row carries) and the timeline looks the call up
by it too, falling back to the old key for a record without the field.
"""

from __future__ import annotations

import json

from primer.session.timeline import build_turn_timeline

T0 = "2026-10-09T10:00:00+00:00"


def _rec(seq: int, kind: str, node_id: str | None = None, **payload) -> str:
    return json.dumps({"seq": seq, "kind": kind, "payload": payload, "created_at": T0, "node_id": node_id})


def _call(seq: int, node: str | None, raw: str = "call_0") -> str:
    return _rec(seq, "tool_call", node, id=f"{node or 'main'}:tool:1:{seq}", raw_id=raw, name="system__invoke_agent", arguments={})


def _llm(seq: int, run: str, node: str | None, *, parent_run: str | None = None, raw: str = "call_0") -> str:
    payload = {"delegated": True, "delegate_tool_call_id": raw, "delegate_run_id": run, "delegate_depth": 1 if parent_run is None else 2, "model": "m", "status": "ok"}
    if parent_run is not None:
        payload["delegate_parent_run_id"] = parent_run
    if node is not None:
        payload["delegate_node_id"] = node
    return _rec(seq, "llm_call", None, **payload)


def _delegated_call(seq: int, run: str, node: str | None, raw: str = "call_0") -> str:
    payload = {"id": f"{run}:tool:1:{seq}", "raw_id": raw, "name": "system__invoke_agent", "arguments": {}, "delegated": True, "delegate_tool_call_id": raw, "delegate_run_id": run, "delegate_depth": 1}
    if node is not None:
        payload["delegate_node_id"] = node
    return _rec(seq, "tool_call", None, **payload)


def _timeline(lines: list[str]) -> dict:
    tl = build_turn_timeline(message_lines=[*lines, _rec(99, "done", None, stop_reason="stop")], turn_log_lines=[], turn_no=0)
    assert tl is not None
    return tl


def _children(tl: dict) -> dict[int, list[int]]:
    """seq of each top-level call -> the seqs of its children."""
    return {c["seq"]: [k["seq"] for k in c["children"]] for c in tl["children"] if c["kind"] == "tool_call"}


def test_two_siblings_that_reuse_one_raw_call_id_keep_their_runs_under_their_own_calls() -> None:
    tl = _timeline([_call(1, "A"), _call(2, "B"), _llm(3, "rA", "A"), _llm(4, "rB", "B")])
    assert _children(tl) == {1: [3], 2: [4]}, tl["children"]


def test_the_runs_may_be_interleaved_as_concurrent_siblings_write_them() -> None:
    tl = _timeline([_call(1, "A"), _call(2, "B"), _llm(3, "rB", "B"), _llm(4, "rA", "A"), _llm(5, "rB", "B"), _llm(6, "rA", "A")])
    assert _children(tl) == {1: [4, 6], 2: [3, 5]}, tl["children"]


def test_a_grandchild_nests_under_its_own_childs_call_inside_its_own_node() -> None:
    lines = [
        _call(1, "A"), _call(2, "B"),
        _delegated_call(3, "rA", "A"), _delegated_call(4, "rB", "B"),
        _llm(5, "gA", "A", parent_run="rA"), _llm(6, "gB", "B", parent_run="rB"),
    ]
    tl = _timeline(lines)
    by_call = {c["seq"]: c for c in tl["children"] if c["kind"] == "tool_call"}
    assert [k["seq"] for k in by_call[1]["children"]] == [3] and [k["seq"] for k in by_call[2]["children"]] == [4], tl["children"]
    assert [g["seq"] for g in by_call[1]["children"][0]["children"]] == [5]
    assert [g["seq"] for g in by_call[2]["children"][0]["children"]] == [6]


def test_a_record_without_a_node_nests_as_it_always_did_under_the_last_call_with_its_raw_id() -> None:
    """The control: a session that is not a graph, or a record from before ``delegate_node_id``."""
    assert _children(_timeline([_call(1, None), _llm(2, "r1", None)])) == {1: [2]}
    old_graph = _children(_timeline([_call(1, "A"), _call(2, "B"), _llm(3, "rA", None)]))
    assert old_graph == {1: [], 2: [3]}, "an old graph record still lands under the last call: the information it never had cannot be recovered"


def test_a_stamped_record_never_nests_under_another_nodes_call() -> None:
    """A record that names its node nests under THAT node's call or nowhere: a call of another node is the wrong answer, not a fallback. It stays where an un-nestable record stays (the root)."""
    tl = _timeline([_call(1, "A"), _llm(2, "r1", "Z")])
    assert _children(tl) == {1: []}, tl["children"]
    assert [c["seq"] for c in tl["children"] if c["kind"] == "llm_call"] == [2]


def test_a_stamped_record_of_a_node_whose_call_comes_later_waits_for_nothing() -> None:
    """The producer writes a call row before the records of its run (the dispatch barrier); a reader does not guess for a record that came first."""
    tl = _timeline([_call(1, "A"), _llm(2, "rB", "B"), _call(3, "B")])
    assert _children(tl) == {1: [], 3: []}, tl["children"]
