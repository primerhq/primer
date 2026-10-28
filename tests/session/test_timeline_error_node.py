"""G12-01: a failed turn's timeline names the failure.

A turn that fails before writing any llm_call/tool_call record (an early
dispatch/LLM-level failure) terminates on an ``error`` record, and the
trace split is exactly where an operator goes to diagnose it. The node
builder (primer/session/timeline.py's ``_tree``) used to drop every kind
except graph_transition/llm_call/tool_call/tool_result/client_action
(``else: continue``), so the failed turn's trace was ``children: []``.

The contract pinned here: the error record writes a terminal node of
kind ``error`` carrying the record's ``code``/``message``, placed by the
house rules (delegated -> under the delegating call, node-attributed ->
under its node, otherwise a root child where it lands last: it IS the
turn's end). The fixture is the auditor's failed call-free turn
(/var/tmp/revamp/audit/G12/probe_g12_01_failed_turn_trace.py).
"""

from __future__ import annotations

import json

from primer.session.timeline import build_turn_timeline

TS0 = "2026-10-09T12:00:00+00:00"
TS1 = "2026-10-09T12:00:02+00:00"


def _failed_call_free_turn() -> tuple[list[str], list[str]]:
    msgs = [
        json.dumps(
            {
                "seq": 1,
                "kind": "user_input",
                "payload": {"text": "do it"},
                "created_at": TS0,
                "node_id": None,
            }
        ),
        json.dumps(
            {
                "seq": 2,
                "kind": "error",
                "payload": {
                    "code": "llm_unavailable",
                    "message": "boom",
                    "node_id": None,
                    "path": None,
                },
                "created_at": TS1,
                "node_id": None,
            }
        ),
    ]
    turn_log = [
        json.dumps({"seq": 1, "kind": "started", "turn_no": 0, "ts": TS0}),
        json.dumps({"seq": 2, "kind": "failed", "turn_no": 0, "ts": TS1}),
    ]
    return msgs, turn_log


def test_a_failed_call_free_turn_carries_an_error_node() -> None:
    msgs, turn_log = _failed_call_free_turn()
    tl = build_turn_timeline(message_lines=msgs, turn_log_lines=turn_log, turn_no=0)
    assert tl is not None, "the failed turn IS one window"
    assert tl["status"] == "failed"

    children = tl.get("children") or []
    errors = [c for c in children if c.get("kind") == "error"]
    assert errors, (
        "the error record writes no node; the trace of a failed, call-free "
        f"turn is empty: {[c.get('kind') for c in children]}"
    )
    node = errors[0]
    assert node["code"] == "llm_unavailable", node
    assert node["message"] == "boom", node
    assert node["seq"] == 2, node
    assert node is children[-1], "the error is the turn's terminal: it lands last"


def test_the_error_node_is_not_doubled_when_calls_also_exist() -> None:
    """A turn that calls a tool and then fails still carries exactly ONE
    error node (the record is one), beside the tool_call it made."""
    msgs, turn_log = _failed_call_free_turn()
    tool_call = json.dumps(
        {
            "seq": 1,
            "kind": "tool_call",
            "payload": {"id": "call_1", "name": "bash", "arguments": {"cmd": "ls"}},
            "created_at": TS0,
            "node_id": None,
        }
    )
    lines = [msgs[0], tool_call, msgs[1]]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=turn_log, turn_no=0)
    assert tl is not None and tl["status"] == "failed"
    children = tl.get("children") or []
    kinds = [c.get("kind") for c in children]
    assert kinds.count("error") == 1, kinds
    assert "tool_call" in kinds, kinds
    assert children[-1].get("kind") == "error", kinds
