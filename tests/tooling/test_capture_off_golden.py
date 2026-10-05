"""scripts/capture_off_golden.py: a re-capture of the off golden may change only the turns it declares.

The guard exists because a fixture that anyone can regenerate after any change pins nothing: the new run is
accepted whatever it says. These tests pin the guard itself (refuse an undeclared change, a declared turn that
did not change, a declaration with no reason), that it records why the fixture moved, and that a turn's unit
does not depend on the turns before it.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests._support.golden_compare import (
    behaviour, call_sha256, changed_turns, differences, turn_units, unit_sha256,
)

ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    spec = importlib.util.spec_from_file_location("capture_off_golden", ROOT / "scripts" / "capture_off_golden.py")
    module = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


script = _load_script()


def _fixture(*turn_calls: int, text: str = "x") -> dict:
    """A fixture whose turn i makes ``turn_calls[i]`` calls, each carrying ``text`` for its turn."""
    turns, calls = [], []
    for i, n in enumerate(turn_calls, start=1):
        turns.append({"turn": f"{i}-t", "llm_calls": n, "file": {"bytes": i}})
        for _ in range(n):
            calls.append({"call": len(calls) + 1, "messages": [f"turn {i}"]})
    return {"captured_from": "a" * 40, "call_count": len(calls), "calls": calls, "turns": turns}


def _with_turn_text(fixture: dict, turn: int, text: str) -> dict:
    out = copy.deepcopy(fixture)
    start = sum(t["llm_calls"] for t in out["turns"][: turn - 1])
    for k in range(out["turns"][turn - 1]["llm_calls"]):
        out["calls"][start + k]["messages"] = [text]
    return out


class TestTheComparison:
    def test_every_differing_path_is_listed_not_only_the_first(self) -> None:
        got = {"a": [1, 2, 3], "b": {"c": "x"}}
        want = {"a": [1, 9, 8], "b": {"c": "y"}}
        assert differences(got, want) == ["/a[1]: 2 != 9", "/a[2]: 3 != 8", "/b/c: 'x' != 'y'"]

    def test_a_turn_that_makes_more_calls_does_not_change_the_turns_after_it(self) -> None:
        """The units number calls from 0 inside the turn, so one turn's extra call is not a cascade."""
        old = _fixture(1, 1, 1)
        new = _fixture(1, 2, 1)
        assert sorted(changed_turns(new, old)) == [2]

    def test_a_change_in_one_turns_calls_is_reported_for_that_turn_only(self) -> None:
        old = _fixture(1, 2, 1)
        assert sorted(changed_turns(_with_turn_text(old, 2, "different"), old)) == [2]

    def test_a_different_number_of_turns_is_reported_as_the_scenario_changing_shape(self) -> None:
        assert list(changed_turns(_fixture(1, 1), _fixture(1, 1, 1))) == [0], "a removed turn: only the shape"

    def test_a_new_turn_is_reported_as_new_beside_the_shape(self) -> None:
        assert list(changed_turns(_fixture(1, 1, 1), _fixture(1, 1))) == [0, 3]

    def test_a_shape_change_does_not_hide_a_change_to_a_turn_both_fixtures_have(self) -> None:
        """Turn 0 used to stand for "everything": declaring it rebaselined every turn unseen."""
        old = _fixture(1, 1)
        new = _with_turn_text(_fixture(1, 1, 1), 1, "different")
        assert sorted(changed_turns(new, old)) == [0, 1, 3]

    def test_metadata_is_not_behaviour(self) -> None:
        fixture = {**_fixture(1), "recaptures": [{"x": 1}]}
        assert set(behaviour(fixture)) == {"call_count", "calls", "turns"}

    def test_a_unit_digest_moves_when_the_unit_does_and_not_when_another_turn_does(self) -> None:
        old = _fixture(1, 1)
        assert unit_sha256(_with_turn_text(old, 2, "z"), 1) == unit_sha256(old, 1)
        assert unit_sha256(_with_turn_text(old, 1, "z"), 1) != unit_sha256(old, 1)
        assert call_sha256(_with_turn_text(old, 1, "z"), 0) != call_sha256(old, 0)
        assert len(turn_units(old)) == 2


class TestTheDecision:
    def _decide(self, *, old, new, declared=(), reason=None, init=False):
        return script.decide(old=old, new=new, declared=list(declared), reason=reason, init=init)[0]

    def test_a_first_capture_needs_init(self) -> None:
        assert self._decide(old=None, new=_fixture(1)).startswith("refuse:")
        assert self._decide(old=None, new=_fixture(1), init=True) == "write-new"

    def test_an_identical_run_only_refreshes_the_metadata(self) -> None:
        assert self._decide(old=_fixture(1, 1), new=_fixture(1, 1)) == "write-metadata"

    def test_a_change_nobody_declared_is_refused(self) -> None:
        old = _fixture(1, 1)
        decision = self._decide(old=old, new=_with_turn_text(old, 2, "z"))
        assert decision.startswith("refuse:") and "[2]" in decision and "not declared" in decision

    def test_a_change_outside_the_declared_turns_is_refused_even_when_one_declared_turn_did_change(self) -> None:
        old = _fixture(1, 1, 1)
        new = _with_turn_text(_with_turn_text(old, 2, "z"), 3, "z")
        decision = self._decide(old=old, new=new, declared=[2], reason="why")
        assert decision.startswith("refuse:") and "[3]" in decision

    def test_a_declared_turn_that_did_not_change_is_refused(self) -> None:
        old = _fixture(1, 1)
        decision = self._decide(old=old, new=_with_turn_text(old, 2, "z"), declared=[1, 2], reason="why")
        assert decision.startswith("refuse:") and "[1]" in decision and "did not change" in decision

    def test_declaring_turns_when_nothing_changed_is_refused(self) -> None:
        assert self._decide(old=_fixture(1), new=_fixture(1), declared=[1], reason="why").startswith("refuse:")

    def test_a_declared_change_needs_a_reason(self) -> None:
        old = _fixture(1, 1)
        assert self._decide(old=old, new=_with_turn_text(old, 2, "z"), declared=[2]) == "refuse:declared changes need --reason"

    def test_declaring_turn_zero_does_not_excuse_the_turns_that_changed_with_the_shape(self) -> None:
        old = _fixture(1, 1)
        new = _with_turn_text(_fixture(1, 1, 1), 1, "different")
        decision = self._decide(old=old, new=new, declared=[0], reason="why")
        assert decision.startswith("refuse:") and "[1, 3]" in decision and "not declared" in decision
        assert self._decide(old=old, new=new, declared=[0, 1, 3], reason="why") == "write-new"

    def test_exactly_the_declared_change_with_a_reason_is_written(self) -> None:
        old = _fixture(1, 1)
        assert self._decide(old=old, new=_with_turn_text(old, 2, "z"), declared=[2], reason="why") == "write-new"


class TestMain:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        path = tmp_path / "golden.json"
        monkeypatch.setattr(script, "FIXTURE", path)
        monkeypatch.setattr(
            script, "_git", lambda *args: "b" * 40 if args[0] == "rev-parse" else "the commit that made it" if args[0] == "log" else "",
        )
        state = {"result": _fixture(1, 1)}
        monkeypatch.setattr(script, "run_scenario", lambda: _coro(state["result"]))
        return path, state

    def test_a_recapture_records_why_the_fixture_moved(self, env) -> None:
        path, state = env
        old = _fixture(1, 1)
        path.write_text(json.dumps(old, indent=1, sort_keys=True) + "\n")
        old_bytes = path.read_text()
        state["result"] = _with_turn_text(old, 2, "z")
        assert script.main(["--expect-changed", "2", "--reason", "turn 2 now keeps its tail"]) == 0
        written = json.loads(path.read_text())
        assert written["captured_from"] == "b" * 40
        assert written["captured_from_subject"] == "the commit that made it", "the handle that survives a rebase"
        assert written["recaptures"] == [{
            "previous_fixture_sha256": hashlib.sha256(old_bytes.encode()).hexdigest(),
            "previous_captured_from": "a" * 40,
            "changed_turns": [2],
            "reason": "turn 2 now keeps its tail",
        }]
        assert written["calls"][1]["messages"] == ["z"]

    def test_a_refused_recapture_writes_nothing(self, env) -> None:
        path, state = env
        old = _fixture(1, 1)
        path.write_text(json.dumps(old, indent=1, sort_keys=True) + "\n")
        before = path.read_text()
        state["result"] = _with_turn_text(old, 2, "z")
        assert script.main([]) == 1
        assert path.read_text() == before

    def test_a_metadata_only_run_keeps_the_history_of_recaptures(self, env) -> None:
        path, state = env
        old = {**_fixture(1, 1), "recaptures": [{"reason": "earlier"}]}
        path.write_text(json.dumps(old, indent=1, sort_keys=True) + "\n")
        assert script.main([]) == 0
        written = json.loads(path.read_text())
        assert written["captured_from"] == "b" * 40 and written["recaptures"] == [{"reason": "earlier"}]
        assert behaviour(written) == behaviour(old)

    def test_check_with_no_fixture_fails_instead_of_looking_like_a_pass(self, env, capsys) -> None:
        path, state = env
        ran: list[int] = []
        state["result"] = _fixture(1, 1)
        original = script.run_scenario
        script.run_scenario = lambda: (ran.append(1), original())[1]
        try:
            assert script.main(["--check"]) == 2
            assert script.main(["--check", "--expect-changed", "1"]) == 2
        finally:
            script.run_scenario = original
        assert not path.exists() and ran == [], "nothing written, and the scenario was not even run"
        assert "no fixture to compare with" in capsys.readouterr().err

    def test_check_never_writes_and_reports_by_exit_code(self, env, capsys) -> None:
        path, state = env
        old = _fixture(1, 1)
        path.write_text(json.dumps(old, indent=1, sort_keys=True) + "\n")
        before = path.read_text()
        state["result"] = _with_turn_text(old, 2, "z")
        assert script.main(["--check"]) == 1
        assert script.main(["--check", "--expect-changed", "2"]) == 0
        assert path.read_text() == before
        assert "turn 2" in capsys.readouterr().out


async def _coro(value):
    return value
