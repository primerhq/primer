"""Delegated runs of concurrent graph siblings nest under their OWN node's call (ticket 01a11cca).

``SH_nestSubagentRows`` (``shell-turns.js``) put a delegated record under the call named by ``delegate_tool_call_id``, the delegating call's RAW provider id, and under the
last call with that id: providers that synthesise ids (``call_0``) reuse them, so two fan-out siblings (graph nodes ``A`` and ``B``) that both delegate had BOTH their runs nested
under ``B``'s call whenever both calls were written before either run's records (they run at once). The recorder now stamps ``delegate_node_id`` on a run's records (the node that
delegated, the same instance-qualified id the parent's call row carries as its ``nodeId``), and the nesting keys on it too. A record without it (a session that is not a graph, or one
written before this) nests exactly as before.

The nesting runs in V8 on the real source; the rows are the shape ``SA_toTranscript`` hands it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL_TURNS = (ROOT / "ui" / "foundation" / "shell-turns.js").read_text(encoding="utf-8")


def _nest(rows: list[dict]) -> list[dict]:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        ctx.eval("var window = globalThis;")
        ctx.eval(SHELL_TURNS)
        return json.loads(ctx.eval(
            "JSON.stringify((function walk(rows) { return rows.map(function (r) { return { seq: r.seq, children: walk(r.children || []) }; }); })"
            f"(SH_nestSubagentRows({json.dumps(rows)})))"
        ))
    finally:
        ctx.close()


def _call(seq: int, node: str | None, raw: str = "call_0", **extra) -> dict:
    payload = {"id": f"{node or 'main'}:tool:1:{seq}", "raw_id": raw, "name": "system__invoke_agent", **extra}
    return {"seq": seq, "kind": "tool_call", "nodeId": node, "payload": payload}


def _delegated(seq: int, run: str, node: str | None, *, parent_run: str | None = None, raw: str = "call_0", kind: str = "assistant_message") -> dict:
    payload = {"text": f"seq {seq}", "delegated": True, "delegate_tool_call_id": raw, "delegate_run_id": run, "delegate_depth": 1 if parent_run is None else 2}
    if parent_run is not None:
        payload["delegate_parent_run_id"] = parent_run
    if node is not None:
        payload["delegate_node_id"] = node
    return {"seq": seq, "kind": kind, "nodeId": None, "payload": payload}


def _tree(nested: list[dict]) -> dict[int, list]:
    """seq -> the seqs of its children, for the top-level rows only (the shape the assertions read)."""
    return {row["seq"]: row["children"] for row in nested}


def test_two_siblings_that_reuse_one_raw_call_id_keep_their_runs_under_their_own_calls() -> None:
    rows = [_call(1, "A"), _call(2, "B"), _delegated(3, "rA", "A"), _delegated(4, "rA", "A"), _delegated(5, "rB", "B"), _delegated(6, "rB", "B")]
    top = _tree(_nest(rows))
    assert [c["seq"] for c in top[1]] == [3, 4], top
    assert [c["seq"] for c in top[2]] == [5, 6], top


def test_the_runs_may_be_interleaved_as_concurrent_siblings_write_them() -> None:
    rows = [_call(1, "A"), _call(2, "B"), _delegated(3, "rB", "B"), _delegated(4, "rA", "A"), _delegated(5, "rB", "B"), _delegated(6, "rA", "A")]
    top = _tree(_nest(rows))
    assert [c["seq"] for c in top[1]] == [4, 6], top
    assert [c["seq"] for c in top[2]] == [3, 5], top


def test_fan_out_instances_of_one_node_are_told_apart_by_their_instance_ids() -> None:
    rows = [_call(1, "worker[0]"), _call(2, "worker[1]"), _delegated(3, "r0", "worker[0]"), _delegated(4, "r1", "worker[1]")]
    top = _tree(_nest(rows))
    assert [c["seq"] for c in top[1]] == [3] and [c["seq"] for c in top[2]] == [4], top


def test_a_grandchild_nests_under_its_own_childs_call_inside_its_own_node() -> None:
    """Node A's child run delegates again, and its call reuses ``call_0`` too; node B has the same shape."""
    rows = [
        _call(1, "A"), _call(2, "B"),
        _delegated(3, "rA", "A"), _delegated(4, "rB", "B"),
        {**_delegated(5, "rA", "A", kind="tool_call"), "payload": {**_delegated(5, "rA", "A")["payload"], "id": "rA:tool:1:1", "raw_id": "call_0", "name": "system__invoke_agent"}},
        {**_delegated(6, "rB", "B", kind="tool_call"), "payload": {**_delegated(6, "rB", "B")["payload"], "id": "rB:tool:1:1", "raw_id": "call_0", "name": "system__invoke_agent"}},
        _delegated(7, "gA", "A", parent_run="rA"), _delegated(8, "gB", "B", parent_run="rB"),
    ]
    top = _tree(_nest(rows))
    a_children = {c["seq"]: [g["seq"] for g in c["children"]] for c in next(r for r in _nest(rows) if r["seq"] == 1)["children"]}
    b_children = {c["seq"]: [g["seq"] for g in c["children"]] for c in next(r for r in _nest(rows) if r["seq"] == 2)["children"]}
    assert a_children == {3: [], 5: [7]}, (a_children, top)
    assert b_children == {4: [], 6: [8]}, (b_children, top)


def test_a_record_without_a_node_nests_as_it_always_did_under_the_last_call_with_its_raw_id() -> None:
    """A session that is not a graph, or a record from before ``delegate_node_id``: the keys it has are the keys it used to need."""
    rows = [_call(1, None), _delegated(2, "r1", None), _delegated(3, "r1", None)]
    assert [c["seq"] for c in _tree(_nest(rows))[1]] == [2, 3]
    graph_old = [_call(1, "A"), _call(2, "B"), _delegated(3, "rA", None)]
    top = _tree(_nest(graph_old))
    assert top[1] == [] and [c["seq"] for c in top[2]] == [3], "an old graph record still lands under the last call: the information it never had cannot be recovered"


def test_a_record_naming_a_node_with_no_call_of_its_own_falls_back_to_the_last_call() -> None:
    """A node id that matches no call row (a renamed instance, a call row not in the window) must not orphan the run."""
    rows = [_call(1, "A"), _delegated(2, "r1", "Z")]
    assert [c["seq"] for c in _tree(_nest(rows))[1]] == [2]


def test_a_call_from_the_parent_turn_and_a_delegated_run_with_the_same_raw_id_do_not_collide() -> None:
    """The control for the run keys: the top-level call's run key is empty, a delegated run's call is keyed by its run."""
    rows = [_call(1, None), _delegated(2, "r1", None), {**_delegated(3, "r1", None, kind="tool_call"), "payload": {**_delegated(3, "r1", None)["payload"], "id": "r1:tool:1:1", "raw_id": "call_0"}},
            _delegated(4, "g1", None, parent_run="r1")]
    nested = _nest(rows)
    assert [c["seq"] for c in nested[0]["children"]] == [2, 3]
    assert [g["seq"] for g in nested[0]["children"][1]["children"]] == [4]


@pytest.mark.parametrize("order", ["calls_first", "sequential"])
def test_the_order_the_siblings_write_in_does_not_matter(order: str) -> None:
    if order == "calls_first":
        rows = [_call(1, "A"), _call(2, "B"), _delegated(3, "rA", "A"), _delegated(4, "rB", "B")]
        parent_a, parent_b = 1, 2
    else:
        rows = [_call(1, "A"), _delegated(2, "rA", "A"), _call(3, "B"), _delegated(4, "rB", "B")]
        parent_a, parent_b = 1, 3
    top = _tree(_nest(rows))
    assert len(top[parent_a]) == 1 and len(top[parent_b]) == 1, top
