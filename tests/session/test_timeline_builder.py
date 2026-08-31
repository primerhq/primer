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
        _rec(3, "tool_call", T1, id="c1", name="bash",
             arguments={"command": "ls -la"}),
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
    assert tool["arguments"] == {"command": "ls -la"}
    assert tool["result"] == {"output": "ok", "error": False, "truncated": False}


def test_errored_tool_result_marks_the_call():
    lines = [
        _rec(1, "tool_call", T0, id="c1", name="bash", arguments="{}"),
        _rec(2, "tool_result", T1, call_id="c1", output="boom", error=True),
        _rec(3, "done", T1, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert tl["children"][0]["status"] == "error"


def test_tool_call_with_no_arguments_key_defaults_to_empty_dict():
    """01a052a5 item 3: the trace panel's expand toggle always has a dict
    to JSON.stringify, never undefined - a call record predating this
    field (or one for a no-arg tool) must not break that."""
    lines = [
        _rec(1, "tool_call", T0, id="c1", name="ping"),
        _rec(2, "tool_result", T1, call_id="c1", output="pong", error=False),
        _rec(3, "done", T1, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert tl["children"][0]["arguments"] == {}


def test_tool_call_with_no_result_yet_has_a_null_result():
    """A still-open call (no paired tool_result record yet) must carry
    the key with a null value, not omit it - dogfood round 2's trace
    overlay reads result unconditionally for every tool_call node."""
    lines = [_rec(1, "tool_call", T0, id="c1", name="bash", arguments={})]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert tl["children"][0]["result"] is None


def test_tool_result_output_is_capped_and_flagged_when_oversized():
    """Dogfood round 2: the trace overlay's expanded form shows results
    alongside arguments - a tool's real output (a read's full file, a
    long command's stdout) can be arbitrarily large, and this is a debug
    view riding the timeline response, not the transcript's own
    paginated rendering, so it needs its own bound."""
    from primer.session.timeline import _RESULT_OUTPUT_CAP

    huge = "x" * (_RESULT_OUTPUT_CAP + 500)
    lines = [
        _rec(1, "tool_call", T0, id="c1", name="read", arguments={}),
        _rec(2, "tool_result", T1, call_id="c1", output=huge, error=False),
        _rec(3, "done", T1, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    result = tl["children"][0]["result"]
    assert len(result["output"]) == _RESULT_OUTPUT_CAP
    assert result["output"] == huge[:_RESULT_OUTPUT_CAP]
    assert result["truncated"] is True
    assert result["error"] is False


def _node_rec(seq, kind, ts, node_id, **payload):
    """Like _rec, but sets the RECORD-level node_id field (as a graph
    fan-out sibling's persisted records genuinely do), not just the
    payload - _tree() reads rec["node_id"], not payload["node_id"], for
    everything except _GRAPH_TRANSITION."""
    return json.dumps({
        "seq": seq, "kind": kind, "payload": payload, "created_at": ts,
        "node_id": node_id,
    })


def test_fanout_siblings_sharing_a_raw_tool_call_id_do_not_overwrite_each_other():
    """01a0518f: THE regression this task exists for, reproduced at the
    timeline level. Two concurrent fan-out sibling nodes can legitimately
    mint the identical raw provider tool_call_id ("call_0" restarts every
    llm.stream() call - persistence.py's own module comment). Before the
    fix, _tree()'s `calls` dict was keyed by the bare id alone: the
    second sibling's TOOL_CALL record would silently overwrite the
    first's entry in that dict, and BOTH tool_result records would then
    pair to whichever entry happened to still be there - one sibling's
    real result, or neither. With scoped ids (persistence.py) plus the
    node-keyed `calls` dict (timeline.py) as defense-in-depth, each
    sibling gets its own entry and its own correctly-paired result."""
    lines = [
        _node_rec(1, "tool_call", T0, "worker[0]", id="call_0", name="fs__read", arguments={}),
        _node_rec(2, "tool_call", T0, "worker[1]", id="call_0", name="fs__read", arguments={}),
        _node_rec(3, "tool_result", T1, "worker[0]", call_id="call_0", output="from sibling 0", error=False),
        _node_rec(4, "tool_result", T1, "worker[1]", call_id="call_0", output="from sibling 1", error=True),
        _rec(5, "done", T2, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)

    tool_calls = [c for c in tl["children"] if c["kind"] == "tool_call"]
    assert len(tool_calls) == 2, (
        f"expected 2 distinct tool_call entries (one per sibling), got "
        f"{len(tool_calls)}: {tool_calls}"
    )
    by_node = {c["node_id"]: c for c in tool_calls}
    assert set(by_node) == {"worker[0]", "worker[1]"}
    # Each sibling's result pairs to ITS OWN call, not the other's.
    assert by_node["worker[0]"]["status"] == "ok"
    assert by_node["worker[0]"]["result"]["output"] == "from sibling 0"
    assert by_node["worker[1]"]["status"] == "error"
    assert by_node["worker[1]"]["result"]["output"] == "from sibling 1"


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
