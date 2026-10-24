"""``gate_is_decided`` / ``undecided_pending_gates``: which pending gates a pending read may still offer (ticket 01a16ca6).

The rows are built by hand in the shape :func:`primer.session.yields.durably_mark_session_resumable` writes, for the cases the API tests in
``tests/api/test_pending_hides_decided_gate.py`` (which drive the real flip) do not reach: a timeout marker, a park from before gates had ids, and a
held wake that names another gate.
"""

from __future__ import annotations

from primer.model.workspace_session import WorkspaceSession
from primer.model.yield_ import with_wake_gate
from primer.session.pending_gates import enumerate_pending_gates, gate_is_decided, undecided_pending_gates
from primer.worker.yield_runtime import make_timeout_payload
from tests.api.test_gate_fence import G1, G2, _approval_session, _graph_two_gate_session

G3 = "c" * 32


def _woken(row: WorkspaceSession, *wakes: tuple[str, dict]) -> WorkspaceSession:
    """``row`` with ``wakes`` (event key, payload) stamped as the flip stamps them: the last one in the singular fields and, on a multi-event park,
    every one in ``resume_event_payloads``."""
    state = dict(row.parked_state or {})
    key, payload = wakes[-1]
    state["resume_event_key"], state["resume_event_payload"] = key, payload
    if row.parked_event_keys:
        state["resume_event_payloads"] = {k.split(":", 2)[2]: {"event_key": k, "payload": p} for k, p in wakes}
    return row.model_copy(update={"parked_status": "resumable", "parked_state": state})


def _gates(row: WorkspaceSession) -> list[dict]:
    return enumerate_pending_gates(row.parked_state or {})


def _ids(gates: list[dict]) -> list[str | None]:
    return [gate["tool_call_id"] for gate in gates]


def test_a_parked_row_holds_no_decision_even_with_a_stale_wake_in_its_state() -> None:
    row = _approval_session(session_id="s", tool_call_id="call-0", gate_id=G1)
    key = row.parked_event_key
    row.parked_state.update(resume_event_key=key, resume_event_payload=with_wake_gate({"decision": "approved"}, G1))

    assert [gate_is_decided(row, gate) for gate in _gates(row)] == [False]
    assert _ids(undecided_pending_gates(row)) == ["call-0"]


def test_a_resumable_single_event_park_has_nothing_left_to_decide_whatever_woke_it() -> None:
    """A single-event park takes one wake: a timeout marker (which names no gate) closes its gate as a decision does."""
    row = _approval_session(session_id="s", tool_call_id="call-0", gate_id=G1)
    timed_out = _woken(row, (row.parked_event_key, make_timeout_payload()))

    assert [gate_is_decided(timed_out, gate) for gate in _gates(timed_out)] == [True]
    assert undecided_pending_gates(timed_out) == []


def test_a_multi_event_park_decides_only_the_gate_its_wake_names() -> None:
    row = _graph_two_gate_session(session_id="g")
    first_key = row.parked_state["graph_checkpoint"]["pending_toolcalls"][0]["parked_event_key"]
    decided = _woken(row, (first_key, with_wake_gate({"decision": "approved"}, G1)))

    assert [gate_is_decided(decided, gate) for gate in _gates(decided)] == [True, False]
    assert [(g["tool_call_id"], g["resume_metadata"]["gate_id"]) for g in undecided_pending_gates(decided)] == [("call-1", G2)]


def test_a_multi_event_park_with_both_gates_decided_offers_nothing() -> None:
    row = _graph_two_gate_session(session_id="g")
    keys = [entry["parked_event_key"] for entry in row.parked_state["graph_checkpoint"]["pending_toolcalls"]]
    decided = _woken(
        row,
        (keys[0], with_wake_gate({"decision": "approved"}, G1)),
        (keys[1], with_wake_gate({"decision": "rejected"}, G2)),
    )

    assert undecided_pending_gates(decided) == []


def test_a_held_wake_that_names_another_gate_is_not_this_gates_decision() -> None:
    """The wake under the key names G3, not the gate (G1) now pending on it: that is not this gate's decision, so it stays on offer."""
    row = _graph_two_gate_session(session_id="g")
    first_key = row.parked_state["graph_checkpoint"]["pending_toolcalls"][0]["parked_event_key"]
    other = _woken(row, (first_key, with_wake_gate({"decision": "approved"}, G3)))

    assert [gate_is_decided(other, gate) for gate in _gates(other)] == [False, False]


def test_a_wake_that_names_no_gate_decides_the_gate_on_its_key() -> None:
    """A timeout marker, or a decision from before gates had ids, names no gate: it decides the gate that waits on its key."""
    row = _graph_two_gate_session(session_id="g")
    second_key = row.parked_state["graph_checkpoint"]["pending_toolcalls"][1]["parked_event_key"]
    timed_out = _woken(row, (second_key, make_timeout_payload()))

    assert [gate_is_decided(timed_out, gate) for gate in _gates(timed_out)] == [False, True]


def test_a_gate_from_before_gates_had_ids_is_judged_by_its_key() -> None:
    row = _graph_two_gate_session(session_id="g")
    for entry in row.parked_state["graph_checkpoint"]["pending_toolcalls"]:
        entry["resume_metadata"].pop("gate_id")
    first_key = row.parked_state["graph_checkpoint"]["pending_toolcalls"][0]["parked_event_key"]
    decided = _woken(row, (first_key, {"decision": "approved"}))

    assert [gate_is_decided(decided, gate) for gate in _gates(decided)] == [True, False]
