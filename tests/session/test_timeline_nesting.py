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
