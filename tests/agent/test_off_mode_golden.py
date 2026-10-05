"""Today's compaction behaviour, pinned byte for byte, for the prompt-budget work's ``off`` mode.

``off`` means "today minus the standalone live-defect fixes", so the fixture moves with each such fix.
History of the fixture (``captured_from`` is the ``primer/`` tree hash it was captured against):

1. captured from the code at ``e22fd42b``;
2. re-captured by the commit that made tier 2 keep its tail and the turn's own input (task 01a1089e): that
   fix changes what a tier-2 turn's marker records and what the next turn is handed, and the scenario's
   turn 2 needed answered history to compact (see ``off_golden.py``). Only turns 2 to 4 (the turns with a
   tier-2 marker) and what they feed changed; turns 1, 5 and 6 and calls 0, 5, 8 and 9 are byte-identical;
3. re-captured, under the guard below, when every turn got its own session (turns 3 and 4 no longer inherit
   turn 2's marker and tail);
4. re-captured for R2 (task 01a10914): the fixed overhead counts, so the markers' figures and the boundary
   turns' padding moved (turns 2 to 6; every prompt of turns 2 to 4 is byte-identical);
5. re-captured when turn 3 was seeded with a prior marker and its kept tail, which gives back the chained
   compaction the per-turn sessions had dropped (only turn 3 moved: it now writes two markers).
   The fixture's ``recaptures`` list says which turns moved and why.

``tests/_support/off_golden.py`` runs one scripted session through every path the budget work
touches (tier 1 pruning, tier 2 summarising, the overflow replay, steers deferred during a
compaction window). ``fixtures/off_mode_golden.json`` is what that session produced, captured by
``scripts/capture_off_golden.py`` (its docstring has the recipe, including how to reproduce an old
fixture). These tests fail when a fresh run differs from it: in any prompt sent to the model, any
marker, the number of LLM calls, or a single byte of the persisted ``messages.jsonl`` after any turn.

Timestamps and the session id are the only values normalised (they differ between runs); everything
else is compared exactly. A change that is MEANT to alter ``off`` behaviour means re-capturing the
fixture in the same commit and saying why in its message. ``scripts/capture_off_golden.py`` is the guard: it
refuses a re-capture that changes a turn not declared with ``--expect-changed``, or declares one that did not
change, or gives no ``--reason``, and records every re-capture on the fixture. Each turn is a unit on its own
session, so one turn's change cannot cascade into the next. Turn 1 and the first call are also pinned by
digest constants below: moving them takes an edit to this file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from primer.model.chat import TextPart
from tests._support.golden_compare import behaviour, call_sha256, changed_turns, differences, unit_sha256
from tests._support.off_golden import (
    PRIOR_SUMMARY, TOOL_RESULT_CHARS, part_fingerprint, run_scenario, trigger_tokens,
)

FIXTURE = Path(__file__).parent / "fixtures" / "off_mode_golden.json"

TURN_1_SHA256 = "d17e974df0cd07f740fda3c7160ba1f05cd5e143810555ce72fe402195710a88"
CALL_0_SHA256 = "97730bbfdd63185dd865548254190931526605a2c5bbdf547638493e7e17719a"


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def runs() -> tuple[dict[str, Any], dict[str, Any]]:
    """Two fresh runs of the scenario (a sync fixture, so each gets its own event loop)."""
    return asyncio.run(run_scenario()), asyncio.run(run_scenario())


class TestTheGolden:
    def test_a_fresh_run_matches_the_fixture_exactly(self, runs, golden) -> None:
        found = differences(runs[0], behaviour(golden))
        assert not found, (
            f"mode `off` no longer behaves as it did for primer/ tree {golden['captured_from'][:8]}: "
            f"{len(found)} differing path(s) in turn(s) {sorted(changed_turns(runs[0], golden))}; the first 20:\n  "
            + "\n  ".join(found[:20])
        )

    def test_the_scenario_is_deterministic(self, runs) -> None:
        """The comparison above is only meaningful if two runs of today's code agree."""
        assert differences(runs[0], runs[1]) == []

    def test_turn_1_and_the_first_call_are_pinned_by_constants_in_this_file(self, golden) -> None:
        """The fixture can be regenerated; these two digests cannot be changed without editing THIS file, in review.

        Turn 1 (tier 1, prune-only) and the first LLM call are the part of ``off`` that no change to compaction
        is meant to touch. If a re-capture moves them, the constants must move with it and say why."""
        assert unit_sha256(golden, 1) == TURN_1_SHA256
        assert call_sha256(golden, 0) == CALL_0_SHA256

    def test_the_fixture_names_the_primer_tree_it_was_captured_from(self, golden) -> None:
        """A tree hash names the exact content of ``primer/`` (a commit SHA is orphaned by a rebase-merge), and the
        commit subject beside it is the handle that survives one. Neither is a pointer into history: after a
        rebase onto a main that has moved the tree may match no commit there (see scripts/capture_off_golden.py)."""
        assert len(golden["captured_from"]) == 40 and set(golden["captured_from"]) <= set("0123456789abcdef")
        assert golden["captured_from_subject"].strip()


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
        assert len(turn["file"]["markers"]) == 2, "the seeded prior marker, then the forced compaction's"
        rejected, summariser, retry = golden["calls"][3:6]
        assert (bool(rejected["tool_ids"]), bool(summariser["tool_ids"]), bool(retry["tool_ids"])) == (True, False, True)

    def test_the_forced_compaction_runs_over_a_reconstructed_summary_and_folds_it(self, golden) -> None:
        """Turn 3's session was compacted before, so its history is [summary, tail, ...] when the forced compaction
        runs: the chain every long session lives in, which the per-turn sessions had stopped pinning."""
        turn = golden["turns"][2]
        first, second = turn["file"]["markers"]
        assert second["seq"] > first["seq"], "the forced marker is written after the seeded one"
        assert "kept_tail" in first and "kept_tail" in second
        assert [k["preview"] for k in first["kept_tail"]] == ["turn 3 earlier question", "turn 3 earlier answer"]
        prior = part_fingerprint(TextPart(text=PRIOR_SUMMARY), "no-session")
        rejected, summariser, retry = golden["calls"][3:6]

        def holds_the_prior_summary(call) -> bool:
            return any(prior in message["parts"] for message in call["messages"])

        assert holds_the_prior_summary(rejected), "the rejected prompt began with the reconstructed summary"
        assert holds_the_prior_summary(summariser), "the summariser read the prior summary with the rest of the head"
        assert not holds_the_prior_summary(retry), "the retry's prompt holds the new summary, which folded the old one"

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
