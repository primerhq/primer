"""S7 section 6: per-node attribution and C1 delegation nesting."""

from __future__ import annotations

import json

from primer.session.timeline import build_turn_timeline


def _rec(seq, kind, ts, node_id=None, **payload):
    return json.dumps({
        "seq": seq, "kind": kind, "payload": payload,
        "created_at": ts, "node_id": node_id,
    })


def _transition(seq, ts, node_id, phase, status=None):
    """A GRAPH_TRANSITION row exactly as persistence.py:310-321 writes it.

    node_id lands BOTH in the payload and on the record: the translator
    sets ``node_id=event.node_id`` alongside ``payload["node_id"]``. The
    builder must key on the payload copy (that is the node the transition
    is ABOUT) with the record field as fallback.
    """
    return json.dumps({
        "seq": seq,
        "kind": "graph_transition",
        "payload": {
            "node_id": node_id,
            "node_kind": "agent",
            "phase": phase,
            "status": status,
        },
        "created_at": ts,
        "node_id": node_id,
    })


T0 = "2026-08-16T10:00:00+00:00"
T1 = "2026-08-16T10:00:01+00:00"
T2 = "2026-08-16T10:00:02+00:00"
T3 = "2026-08-16T10:00:03+00:00"


def test_graph_nodes_group_their_own_children():
    lines = [
        _transition(1, T0, "n1", "enter"),
        _rec(2, "llm_call", T1, node_id="n1", profile_id="p", provider_id="v",
             model="m", input_tokens=1, output_tokens=1, duration_ms=10,
             status="ok"),
        _transition(3, T2, "n1", "exit", status="ok"),
        _rec(4, "done", T3, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert [c["kind"] for c in tl["children"]] == ["node"]
    node = tl["children"][0]
    assert node["node_id"] == "n1"
    assert node["status"] == "ok"
    assert node["duration_ms"] == 2000
    assert [c["kind"] for c in node["children"]] == ["llm_call"]


def test_records_outside_a_node_stay_at_the_root():
    lines = [
        _rec(1, "llm_call", T0, profile_id="p", provider_id="v", model="m",
             input_tokens=1, output_tokens=1, duration_ms=10, status="ok"),
        _transition(2, T1, "n1", "enter"),
        _transition(3, T2, "n1", "exit", status="ok"),
        _rec(4, "done", T3, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert [c["kind"] for c in tl["children"]] == ["llm_call", "node"]


def test_delegated_records_nest_under_the_delegating_tool_call():
    """C1: an inline subagent's calls are recorded on the PARENT log."""
    lines = [
        _rec(1, "tool_call", T0, id="c1", name="system__invoke_agent",
             arguments="{}"),
        _rec(2, "llm_call", T1, delegated=True, delegate_tool_call_id="c1",
             profile_id="child", provider_id="v", model="m", input_tokens=3,
             output_tokens=2, duration_ms=50, status="ok"),
        _rec(3, "tool_result", T2, call_id="c1", output="ok", error=False),
        _rec(4, "llm_call", T2, profile_id="parent", provider_id="v",
             model="m", input_tokens=9, output_tokens=1, duration_ms=20,
             status="ok"),
        _rec(5, "done", T3, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert [c["kind"] for c in tl["children"]] == ["tool_call", "llm_call"]
    delegating = tl["children"][0]
    assert delegating["status"] == "ok"
    assert [c["profile_id"] for c in delegating["children"]] == ["child"]
    assert tl["children"][1]["profile_id"] == "parent"


def test_delegated_record_with_an_unknown_call_id_stays_at_the_root():
    lines = [
        _rec(1, "llm_call", T0, delegated=True, delegate_tool_call_id="gone",
             profile_id="child", provider_id="v", model="m", input_tokens=1,
             output_tokens=1, duration_ms=5, status="ok"),
        _rec(2, "done", T1, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert [c["kind"] for c in tl["children"]] == ["llm_call"]


def _delegated_llm(seq, ts, *, run, parent, delegate, profile):
    return _rec(seq, "llm_call", ts, delegated=True, delegate_tool_call_id=delegate, delegate_run_id=run,
                delegate_parent_run_id=parent, delegate_depth=1 if parent is None else 2,
                profile_id=profile, provider_id="v", model="m", input_tokens=1, output_tokens=1, duration_ms=5, status="ok")


def test_nested_runs_that_reuse_one_raw_call_id_nest_by_run_not_by_the_last_call_with_that_id():
    """The child's own call to the grandchild reuses the parent's raw id ``call_0`` (providers that synthesise ids restart the
    numbering every stream). A raw-id lookup replaces the parent's entry with the child's, so the child's LATER records nested
    under its own call instead of the parent's. Looked up by the run that made the delegating call, each lands where it belongs."""
    lines = [
        _rec(1, "tool_call", T0, id="call_0", name="system__invoke_agent", arguments="{}"),
        _delegated_llm(2, T0, run="R1", parent=None, delegate="call_0", profile="child-before"),
        _rec(3, "tool_call", T1, id="call_0", name="system__invoke_agent", arguments="{}", delegated=True,
             delegate_tool_call_id="call_0", delegate_run_id="R1", delegate_parent_run_id=None, delegate_depth=1),
        _delegated_llm(4, T1, run="R2", parent="R1", delegate="call_0", profile="grandchild"),
        _delegated_llm(5, T2, run="R1", parent=None, delegate="call_0", profile="child-after"),
        _rec(6, "done", T3, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    (parents_call,) = tl["children"]
    assert [c.get("profile_id") or c["kind"] for c in parents_call["children"]] == ["child-before", "tool_call", "child-after"]
    childs_call = parents_call["children"][1]
    assert [c["profile_id"] for c in childs_call["children"]] == ["grandchild"]


def test_a_delegated_record_with_a_run_id_whose_delegating_call_is_missing_is_not_nested_under_a_wrong_call():
    """Exact by run: a call with the same raw id made by ANOTHER run is not the one that delegated."""
    lines = [
        _rec(1, "tool_call", T0, id="call_0", name="system__invoke_agent", arguments="{}"),
        _delegated_llm(2, T1, run="R9", parent="R-not-seen", delegate="call_0", profile="orphan"),
        _rec(3, "done", T3, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert [c["kind"] for c in tl["children"]] == ["tool_call", "llm_call"]
    assert tl["children"][0]["children"] == []


def test_records_written_before_run_ids_existed_still_nest_by_raw_id():
    lines = [
        _rec(1, "tool_call", T0, id="c1", name="system__invoke_agent", arguments="{}"),
        _rec(2, "llm_call", T1, delegated=True, delegate_tool_call_id="c1", profile_id="old", provider_id="v", model="m",
             input_tokens=1, output_tokens=1, duration_ms=5, status="ok"),
        _rec(3, "done", T3, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert [c["profile_id"] for c in tl["children"][0]["children"]] == ["old"]


def test_an_unclosed_node_still_renders():
    lines = [
        _transition(1, T0, "n1", "enter"),
        _rec(2, "tool_call", T1, node_id="n1", id="c1", name="bash",
             arguments="{}"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    node = tl["children"][0]
    assert node["ended_at"] is None
    assert node["duration_ms"] is None
    assert len(node["children"]) == 1
