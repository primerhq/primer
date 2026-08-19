"""S7 section 6: fold one turn's records into a tree."""

from __future__ import annotations

import json

from primer.session.timeline import build_turn_timeline


def _rec(seq, kind, ts, **payload):
    return json.dumps({
        "seq": seq, "kind": kind, "payload": payload, "created_at": ts,
    })


def _ev(seq, kind, turn_no, ts, **extra):
    return json.dumps({
        "seq": seq, "kind": kind, "turn_no": turn_no, "ts": ts, **extra,
    })


T0 = "2026-08-16T10:00:00+00:00"
T1 = "2026-08-16T10:00:01+00:00"
T2 = "2026-08-16T10:00:02+00:00"
T3 = "2026-08-16T10:00:03+00:00"


def _agent_turn():
    return [
        _rec(1, "user_input", T0, text="hi"),
        _rec(2, "llm_call", T1, profile_id="prof-1", provider_id="prov-1",
             model="m-1", input_tokens=11, output_tokens=7, duration_ms=900,
             status="ok"),
        _rec(3, "tool_call", T1, id="c1", name="bash", arguments="{}"),
        _rec(4, "done", T1, stop_reason="tool_use"),
        _rec(5, "tool_result", T2, call_id="c1", output="ok", error=False),
        _rec(6, "llm_call", T2, profile_id="prof-1", provider_id="prov-1",
             model="m-1", input_tokens=20, output_tokens=4, duration_ms=400,
             status="ok"),
        _rec(7, "assistant_token", T3, text="all done"),
        _rec(8, "done", T3, stop_reason="stop"),
    ]


def test_out_of_range_turn_is_none():
    assert build_turn_timeline(
        message_lines=_agent_turn(), turn_log_lines=[], turn_no=4,
    ) is None


def test_children_are_the_calls_and_tool_pairs_in_seq_order():
    tl = build_turn_timeline(
        message_lines=_agent_turn(), turn_log_lines=[], turn_no=0,
    )
    kinds = [c["kind"] for c in tl["children"]]
    assert kinds == ["llm_call", "tool_call", "llm_call"]
    assert [c["seq"] for c in tl["children"]] == [2, 3, 6]


def test_llm_call_children_carry_the_call_payload():
    tl = build_turn_timeline(
        message_lines=_agent_turn(), turn_log_lines=[], turn_no=0,
    )
    call = tl["children"][0]
    assert call["profile_id"] == "prof-1"
    assert call["provider_id"] == "prov-1"
    assert call["input_tokens"] == 11
    assert call["output_tokens"] == 7
    assert call["duration_ms"] == 900
    assert call["status"] == "ok"


def test_tool_result_folds_into_its_call():
    tl = build_turn_timeline(
        message_lines=_agent_turn(), turn_log_lines=[], turn_no=0,
    )
    tool = tl["children"][1]
    assert tool["tool_call_id"] == "c1"
    assert tool["name"] == "bash"
    assert tool["status"] == "ok"
    assert tool["duration_ms"] == 1000


def test_errored_tool_result_marks_the_call():
    lines = [
        _rec(1, "tool_call", T0, id="c1", name="bash", arguments="{}"),
        _rec(2, "tool_result", T1, call_id="c1", output="boom", error=True),
        _rec(3, "done", T1, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert tl["children"][0]["status"] == "error"


def test_client_action_is_a_leaf_under_the_call_it_delivered():
    """S3 writes one delivery record per notifying call into this log."""
    lines = [
        _rec(1, "tool_call", T0, id="c1", name="client__open_file",
             arguments="{}"),
        _rec(2, "client_action", T1, call_id="c1", name="client__open_file",
             arguments={}),
        _rec(3, "tool_result", T1, call_id="c1", output="ok", error=False),
        _rec(4, "done", T1, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert [c["kind"] for c in tl["children"]] == ["tool_call"]
    assert [c["kind"] for c in tl["children"][0]["children"]] == ["client_action"]
    assert tl["children"][0]["children"][0]["name"] == "client__open_file"


def test_the_envelope_echoes_the_windows_terminal_seq():
    """Carried so a caller can re-resolve a turn without the ordinal."""
    tl = build_turn_timeline(
        message_lines=_agent_turn(), turn_log_lines=[], turn_no=0,
    )
    assert tl["terminal_seq"] == 8


def test_envelope_supplies_status_and_timing():
    log = [
        _ev(1, "started", 0, T0, model="m-1", input_message_count=1),
        _ev(2, "completed", 0, T3, duration_ms=3000, finish_reason="stop"),
    ]
    tl = build_turn_timeline(
        message_lines=_agent_turn(), turn_log_lines=log, turn_no=0,
    )
    assert tl["turn_no"] == 0
    assert tl["status"] == "completed"
    assert tl["started_at"] == T0
    assert tl["ended_at"] == T3
    assert tl["duration_ms"] == 3000


def test_status_falls_back_to_the_records_without_a_turn_log():
    tl = build_turn_timeline(
        message_lines=_agent_turn(), turn_log_lines=[], turn_no=0,
    )
    assert tl["status"] == "completed"
    assert tl["started_at"] == T0
    assert tl["ended_at"] == T3


def test_failed_turn_status():
    lines = [
        _rec(1, "user_input", T0, text="hi"),
        _rec(2, "error", T1, message="boom", code="x"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert tl["status"] == "failed"


def test_open_turn_status_is_running():
    lines = [_rec(1, "user_input", T0, text="hi")]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert tl["status"] == "running"
