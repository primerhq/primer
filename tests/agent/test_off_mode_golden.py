"""Today's compaction behaviour, pinned byte for byte, for the prompt-budget work's ``off`` mode.

``off`` means "today minus the standalone live-defect fixes", so the fixture moves with each such fix:
it was first captured at ``e22fd42b`` and re-captured at the commit that made tier 2 keep its tail and the
turn's own input (that fix changes what a tier-2 turn sends, what its marker records and what the next
turn is handed; the scenario's turn 2 also needed answered history to compact, see ``off_golden.py``).

``tests/_support/off_golden.py`` runs one scripted session through every path the budget work
touches (tier 1 pruning, tier 2 summarising, the overflow replay, steers deferred during a
compaction window). ``fixtures/off_mode_golden.json`` is what that session produced on the code at
the pinned commit, captured by ``scripts/capture_off_golden.py`` from a clean checkout of it. These
tests fail when a fresh run differs from it: in any prompt sent to the model, any marker, the number
of LLM calls, or a single byte of the persisted ``messages.jsonl`` after any turn.

Timestamps and the session id are the only values normalised (they differ between runs); everything
else is compared exactly. A change that is MEANT to alter ``off`` behaviour (there should be none in
the prompt-budget work, which has one intended recorded-figure difference outside this path) means
re-capturing the fixture from a clean checkout in the same commit, and saying why in its message.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests._support.off_golden import TOOL_RESULT_CHARS, run_scenario, trigger_tokens

FIXTURE = Path(__file__).parent / "fixtures" / "off_mode_golden.json"


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def runs() -> tuple[dict[str, Any], dict[str, Any]]:
    """Two fresh runs of the scenario (a sync fixture, so each gets its own event loop)."""
    return asyncio.run(run_scenario()), asyncio.run(run_scenario())


def _first_difference(got: Any, want: Any, path: str = "") -> str | None:
    if type(got) is not type(want):
        return f"{path or '/'}: type {type(got).__name__} != {type(want).__name__}"
    if isinstance(got, dict):
        for key in sorted(set(got) | set(want)):
            if key not in got or key not in want:
                return f"{path}/{key}: present in only one of them"
            found = _first_difference(got[key], want[key], f"{path}/{key}")
            if found:
                return found
    elif isinstance(got, list):
        if len(got) != len(want):
            return f"{path}: {len(got)} items != {len(want)}"
        for i, (a, b) in enumerate(zip(got, want)):
            found = _first_difference(a, b, f"{path}[{i}]")
            if found:
                return found
    elif got != want:
        return f"{path}: {got!r} != {want!r}"
    return None


class TestTheGolden:
    def test_a_fresh_run_matches_the_fixture_exactly(self, runs, golden) -> None:
        difference = _first_difference(runs[0], {k: v for k, v in golden.items() if k != "captured_from"})
        assert difference is None, (
            f"mode `off` no longer behaves as it did at {golden['captured_from'][:8]}: first difference at {difference}"
        )

    def test_the_scenario_is_deterministic(self, runs) -> None:
        """The comparison above is only meaningful if two runs of today's code agree."""
        assert _first_difference(runs[0], runs[1]) is None

    def test_the_fixture_names_the_commit_it_was_captured_from(self, golden) -> None:
        assert len(golden["captured_from"]) == 40 and set(golden["captured_from"]) <= set("0123456789abcdef")


class TestTheFixtureCrossesWhatItPins:
    """A golden that never reached a path pins nothing about it, so read the paths off the fixture."""

    def test_tier_1_prunes_in_memory_and_persists_nothing(self, golden) -> None:
        turn = golden["turns"][0]
        assert turn["llm_calls"] == 1
        assert turn["file"]["markers"] == [], "a prune-only compaction writes no marker"
        # the file still holds the four raw tool results...
        assert turn["file"]["bytes"] >= 4 * TOOL_RESULT_CHARS
        assert [l["preview"] for l in turn["file"]["lines"] if l.get("message") == "tool"] == [
            f"<tool_result call_{i} {TOOL_RESULT_CHARS} chars>" for i in range(4)
        ]
        # ...while the prompt the model was sent carried them as short placeholders
        sent = [p[1] for m in golden["calls"][0]["messages"] if m["role"] == "tool" for p in m["parts"]]
        assert len(sent) == 4 and all(length < 1_000 for length in sent)

    def test_tier_2_summarises_and_writes_one_marker(self, golden) -> None:
        turn = golden["turns"][1]
        assert turn["llm_calls"] == 2
        assert len(turn["file"]["markers"]) == 1
        summariser, answer = golden["calls"][1], golden["calls"][2]
        assert summariser["tool_ids"] == [] and answer["tool_ids"] != []

    def test_the_tier_2_marker_keeps_the_tail_and_the_input_the_turn_answers(self, golden) -> None:
        marker = golden["turns"][1]["file"]["markers"][0]
        kept = marker["kept_tail"]
        assert kept[-1]["preview"] == "turn 2: next", "the unanswered input is the last thing the marker keeps"
        assert [k["message"] for k in kept] == ["assistant", "user", "assistant", "user"][-len(kept):]
        assert 2 <= len(kept) < 9, "a tail, bounded by size: not the whole history and not nothing"
        later = golden["turns"][2]["file"]["markers"]
        assert all("kept_tail" in m for m in later), "every tier-2 marker records its tail"

    def test_the_overflow_replay_force_compacts_and_reruns_the_loop(self, golden) -> None:
        turn = golden["turns"][2]
        assert turn["llm_calls"] == 3
        assert len(turn["file"]["markers"]) == 2
        rejected, summariser, retry = golden["calls"][3:6]
        assert (bool(rejected["tool_ids"]), bool(summariser["tool_ids"]), bool(retry["tool_ids"])) == (True, False, True)

    def test_steers_during_compaction_land_after_the_marker_in_order_and_the_turn_never_sees_them(self, golden) -> None:
        turn = golden["turns"][3]
        lines = turn["file"]["lines"]
        marker = max(i for i, l in enumerate(lines) if l.get("record") == "compaction_marker")
        assert [l["preview"] for l in lines[marker + 1:]] == [
            "STEER-DURING-COMPACTION-1", "STEER-DURING-COMPACTION-2", "turn-4-ok",
        ]
        turn_call = golden["calls"][7]
        assert turn_call["messages"][-1]["role"] == "assistant", "the in-flight turn is not handed the deferred steers"

    def test_every_scripted_llm_call_is_accounted_for(self, golden) -> None:
        assert golden["call_count"] == 10 == sum(t["llm_calls"] for t in golden["turns"])
        assert [c["call"] for c in golden["calls"] if not c["tool_ids"]] == [2, 5, 7], "the three summariser calls"

    def test_call_arguments_are_recorded_by_value_and_tools_by_digest(self, golden) -> None:
        """A change to temperature, max_output_tokens or a tool's schema must show, not only a change of tool ids."""
        turn_call, summariser = golden["calls"][0], golden["calls"][1]
        assert turn_call["kwargs"] == {"max_output_tokens": "None", "temperature": "None", "tool_choice": "'auto'"}
        assert summariser["kwargs"] == {"max_output_tokens": "4096", "temperature": "0.0"}
        assert turn_call["response_format"] is None
        assert [t[0] for t in turn_call["tools"]] == turn_call["tool_ids"] and len(turn_call["tools"]) == 7
        assert all(isinstance(length, int) and length > 50 and len(digest) == 16 for _id, length, digest in turn_call["tools"])
        assert summariser["tools"] == []

    def test_the_boundary_turns_pin_that_the_trigger_fires_at_the_trigger_not_one_under(self, golden) -> None:
        under, at = golden["turns"][4], golden["turns"][5]
        trigger = trigger_tokens()
        assert (under["target_estimate"], at["target_estimate"]) == (trigger - 1, trigger)
        assert under["llm_calls"] == at["llm_calls"] == 1
        assert under["file"]["markers"] == at["file"]["markers"] == [], "tier 1 alone suffices: no marker in either"
        sent = lambda call: [p[1] for m in call["messages"] if m["role"] == "tool" for p in m["parts"]]  # noqa: E731
        assert all(length > 50_000 for length in sent(golden["calls"][8])), "one token under: the raw results are sent"
        assert all(length < 1_000 for length in sent(golden["calls"][9])), "at the trigger: they are pruned in memory"
