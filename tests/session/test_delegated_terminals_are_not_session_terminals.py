"""A delegated run's terminal record ends the SUBAGENT's turn, never the session's (ticket 01a11232).

A subagent's records are written inline into the parent session's ``messages.jsonl`` (``payload.delegated``), and each
delegated run ends with a ``done`` of its own. Three readers decided "this record closes a turn" on their own and none
asked whether it was delegated: ``timeline.closes_turn`` (the turn windows and the trace's turn ordinals),
``turns.count_turn_state`` (open-turn detection) and the final-result relay's boundary scan. They now share one
predicate, :mod:`primer.session.terminals`.

The log under test is built by the same code that writes one (``tests/ui_e2e/_delegation_seed.py``: the persistence
layer's own translation for the parent, the real ``DelegationRecorder`` for the delegated records): a parent turn that
calls a subagent (run R1), which calls another (run R2).
"""

from __future__ import annotations

import copy
import json

import pytest

from primer.channel.session_relay import _parse_tail, derive_session_final_text
from primer.session.terminals import is_session_terminal
from primer.session.timeline import _turn_status, closes_turn
from primer.session.turns import count_turn_state, has_open_turn
from tests.ui_e2e._delegation_seed import PARENT_FINAL, CHILD_AFTER, build


@pytest.fixture(scope="module")
def seeded() -> list[dict]:
    return build().records


def _lines(records: list[dict]) -> list[str]:
    return [json.dumps(r) for r in records]


def _kinds(records: list[dict]) -> list[tuple[int, str]]:
    return [(r["seq"], r["kind"]) for r in records]


def _through_the_last_delegated_done(records: list[dict]) -> list[dict]:
    """The log up to and including the last delegated run's ``done`` (cut by what the record is, not by a position the seed's record count would move)."""
    last = max(i for i, r in enumerate(records) if r["kind"] == "done" and r["payload"].get("delegated"))
    return records[: last + 1]


def _with_the_parents_tool_round_done(records: list[dict]) -> list[dict]:
    """The log a real parent turn leaves: the model call that issued the delegating tool call ENDS with its own
    ``done(stop_reason=tool_use)`` (persistence writes one per model call), before the tool runs and the delegated
    records appear. The seed does not carry it, so insert it right after the parent's call and renumber."""
    out: list[dict] = []
    inserted = False
    for rec in copy.deepcopy(records):
        out.append(rec)
        if rec["kind"] == "tool_call" and not inserted:
            inserted = True
            out.append({
                "seq": 0, "kind": "done", "payload": {"stop_reason": "tool_use", "raw_reason": "tool_calls"},
                "created_at": rec["created_at"],
            })
    for seq, rec in enumerate(out, start=1):
        rec["seq"] = seq
    return out


# ---------------------------------------------------------------------------
# closes_turn
# ---------------------------------------------------------------------------


def test_only_the_parents_final_done_closes_the_turn(seeded) -> None:
    terminals = [r["seq"] for r in seeded if r["kind"] in ("done", "error", "cancelled")]
    assert len(terminals) == 3, "the seed has two delegated dones and the parent's own"
    assert [r["seq"] for r in seeded if closes_turn(r)] == [terminals[-1]]


@pytest.mark.parametrize("kind", ["done", "error", "cancelled"])
def test_a_delegated_terminal_of_any_kind_never_closes_the_session_turn(kind) -> None:
    assert closes_turn({"kind": kind, "payload": {"delegated": True, "delegate_run_id": "r1"}}) is False
    assert closes_turn({"kind": kind, "payload": {}}) is True


def test_a_tool_round_done_still_does_not_close_the_turn() -> None:
    assert closes_turn({"kind": "done", "payload": {"stop_reason": "tool_use"}}) is False


@pytest.mark.parametrize(("kind", "wrong"), [("error", "failed"), ("cancelled", "cancelled")])
def test_a_subagents_failure_does_not_mark_the_running_parent_turn_failed(kind, wrong) -> None:
    """With no turn-log event to go by, the window's status is read from its last record. A subagent that failed or was
    cancelled while its parent carries on leaves a delegated error / cancelled as that record."""
    records = [
        {"seq": 1, "kind": "user_input", "payload": {"text": "go"}},
        {"seq": 2, "kind": kind, "payload": {"delegated": True, "delegate_run_id": "r1"}},
    ]
    assert _turn_status([], records) == "running"
    records[1]["payload"] = {}
    assert _turn_status([], records) == wrong, "the same record written by the session's own run still decides it"


def test_a_subagents_done_does_not_mark_the_running_parent_turn_completed(seeded) -> None:
    before_the_final_done = _through_the_last_delegated_done(seeded)
    assert before_the_final_done[-1]["kind"] == "done" and before_the_final_done[-1]["payload"].get("delegated")
    assert _turn_status([], before_the_final_done) == "running"
    assert _turn_status([], seeded) == "completed"


def test_a_done_that_says_the_tool_turn_cap_stopped_the_turn_closes_it() -> None:
    """The capped round's ``done`` carries ``stop_reason="tool_turn_cap"`` (#437). It is NOT ``tool_use``: the run stopped there, so
    it ends the turn, and the shared rule must not treat it as one more tool round."""
    capped = {"kind": "done", "payload": {"stop_reason": "tool_turn_cap", "raw_reason": "tool_use"}}
    assert closes_turn(capped) is True
    assert is_session_terminal(capped) is True
    lines = [
        json.dumps({"seq": 1, "kind": "user_input", "payload": {"text": "go"}}),
        json.dumps({"seq": 2, "kind": "done", "payload": {"stop_reason": "tool_use"}}),
        json.dumps({"seq": 3, **capped}),
    ]
    assert count_turn_state(lines, cursor=1).terminals == 1, "the tool round does not close the turn, the capped done does"
    assert has_open_turn(lines, cursor=1) is False
    assert has_open_turn(lines[:2], cursor=1) is True


# ---------------------------------------------------------------------------
# count_turn_state / has_open_turn
# ---------------------------------------------------------------------------


def test_the_delegated_dones_are_not_counted_against_the_users_input(seeded) -> None:
    tc = count_turn_state(_lines(seeded), cursor=1)
    assert (tc.open_user_inputs, tc.terminals) == (1, 1)


def test_a_turn_with_a_delegated_run_is_still_open_before_its_own_final_done(seeded) -> None:
    before_the_final_done = seeded[:-1]
    assert before_the_final_done[-1]["kind"] == "assistant_token"
    assert has_open_turn(_lines(before_the_final_done), cursor=1) is True, (
        "the two delegated dones must not close the parent's turn"
    )
    assert has_open_turn(_lines(seeded), cursor=1) is False


def test_a_tool_round_done_is_not_a_terminal_for_the_open_turn_count_either() -> None:
    """The same predicate: a model call that ended in a tool call (``done(tool_use)``) is the end of a ROUND, not of the
    user's turn. Counted, it closed every tool-using turn the moment its first round finished."""
    lines = [
        json.dumps({"seq": 1, "kind": "user_input", "payload": {"text": "hi"}}),
        json.dumps({"seq": 2, "kind": "done", "payload": {"stop_reason": "tool_use"}}),
    ]
    assert count_turn_state(lines, cursor=1).terminals == 0
    assert has_open_turn(lines, cursor=1) is True


# ---------------------------------------------------------------------------
# The final-result relay
# ---------------------------------------------------------------------------


def test_a_parent_that_died_after_a_delegated_done_has_no_final_result(seeded) -> None:
    """The parent's turn never ended (its ``cancelled``/``error`` write is best-effort and can be skipped), and the last
    thing in the log is a delegated run's ``done``. That ``done`` used to be the last boundary, so the SUBAGENT's last
    words were relayed as the session's result."""
    crashed = _through_the_last_delegated_done(_with_the_parents_tool_round_done(seeded))
    assert _kinds(crashed)[-1][1] == "done" and crashed[-1]["payload"].get("delegated")
    assert derive_session_final_text(crashed) is None


def test_the_relayed_text_of_a_completed_parent_turn_is_the_parents_not_the_subagents(seeded) -> None:
    completed = _with_the_parents_tool_round_done(seeded)
    assert derive_session_final_text(completed) == PARENT_FINAL


def test_the_final_text_stands_when_the_parent_answers_after_the_delegated_run(seeded) -> None:
    """Control (green before and after): nothing changes for the common shape."""
    assert derive_session_final_text(seeded) == PARENT_FINAL
    assert CHILD_AFTER not in derive_session_final_text(seeded)


def test_the_tail_parse_reaches_back_past_delegated_terminals(seeded) -> None:
    """``_parse_tail`` stops after two window boundaries. A delegated ``done`` is not one, so the tail must still start at
    the parent's own previous boundary (its tool-round ``done``) and not at a subagent's."""
    completed = _with_the_parents_tool_round_done(seeded)
    tail = _parse_tail(reversed(_lines(completed)))
    first_boundary = next(r for r in tail if r["kind"] == "done")
    assert not first_boundary["payload"].get("delegated"), "the tail was cut at a delegated run's done"
    assert first_boundary["payload"].get("stop_reason") == "tool_use"
