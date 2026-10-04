"""primer.agent.prune: sticky, position-stable, caller-sized, envelope-preserving."""

from __future__ import annotations

import re

import pytest

from primer.agent.prune import (
    PruneSet,
    apply_prune_set,
    default_size,
    prune_prompt,
    result_keys,
)
from primer.model.chat import (
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


def _user(text: str = "go") -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _call(call_id: str) -> Message:
    return Message(role="assistant", parts=[ToolCallPart(id=call_id, name="read", arguments={"p": call_id})])


def _result(call_id: str, output: str, **extra) -> Message:
    return Message(role="tool", parts=[ToolResultPart(id=call_id, output=output, **extra)])


def _outputs(messages: list[Message]) -> list[str]:
    return [
        p.output for m in messages if m.role == "tool"
        for p in m.parts if isinstance(p, ToolResultPart)
    ]


def _rounds(*sizes_chars: int) -> list[Message]:
    """user, then (call, result) per size; every call id is 'call_0' like a real adapter."""
    out = [_user()]
    for i, n in enumerate(sizes_chars):
        out += [_call("call_0"), _result("call_0", chr(ord("a") + i) * n)]
    return out


BIG = 40_000  # ~10k tokens by the heuristic


def _is_placeholder(text: str) -> bool:
    return text.startswith("[output of") and "omitted to fit the context window" in text


class TestShedding:
    def test_replaces_the_largest_old_result_first_and_stops_when_enough_is_shed(self) -> None:
        msgs = _rounds(BIG, BIG * 2, BIG, BIG)  # four rounds; the newest two are kept
        out = prune_prompt(msgs, shed_tokens=default_size(msgs[4].parts[0]) - 100)
        outs = _outputs(out.messages)
        assert outs[1].startswith("[output of 80000 chars omitted"), "the biggest old result goes first"
        assert outs[0] == "a" * BIG, "enough was shed after one: no over-pruning"
        assert outs[2:] == ["c" * BIG, "d" * BIG]

    def test_the_newest_rounds_are_never_replaced(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        outs = _outputs(prune_prompt(msgs, shed_tokens=10**9, keep_rounds=2).messages)
        assert _is_placeholder(outs[0])
        assert outs[1:] == ["b" * BIG, "c" * BIG]

    def test_results_under_the_minimum_are_left_alone(self) -> None:
        msgs = _rounds(400, 400, BIG, BIG)
        out = prune_prompt(msgs, shed_tokens=10**9, min_tokens=1_000)
        assert _outputs(out.messages)[:2] == ["a" * 400, "b" * 400]

    def test_it_reports_what_it_shed_and_what_it_added(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        out = prune_prompt(msgs, shed_tokens=1)
        keys = result_keys(msgs)
        assert out.added.omitted == {keys[(2, 0)]}
        assert out.shed_tokens == default_size(msgs[2].parts[0]) - default_size(out.messages[2].parts[0])
        assert out.shed_by_sticky == 0

    def test_nothing_to_shed_is_a_no_op(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        out = prune_prompt(msgs, shed_tokens=0)
        assert out.messages == msgs and not out.prune_set and out.shed_tokens == 0

    @pytest.mark.parametrize("keep_rounds", [0, 1, 3])
    def test_keep_rounds_bounds_what_is_protected(self, keep_rounds: int) -> None:
        msgs = _rounds(BIG, BIG, BIG, BIG)
        outs = _outputs(prune_prompt(msgs, shed_tokens=10**9, keep_rounds=keep_rounds).messages)
        assert [_is_placeholder(o) for o in outs] == [i < 4 - keep_rounds for i in range(4)]

    @pytest.mark.parametrize("keep_rounds", [4, 5, 9])
    def test_keep_rounds_beyond_the_number_of_rounds_protects_all_of_them(self, keep_rounds: int) -> None:
        """A negative start index used to wrap and protect only the last few."""
        msgs = _rounds(BIG, BIG, BIG, BIG)
        out = prune_prompt(msgs, shed_tokens=10**9, keep_rounds=keep_rounds)
        assert _outputs(out.messages) == _outputs(msgs) and not out.prune_set


class TestEnvelope:
    def test_only_output_text_changes(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        msgs[2] = Message(role="tool", parts=[ToolResultPart(
            id="call_0", output="z" * BIG, error=True, metadata={"match_count": 3},
        )])
        part = prune_prompt(msgs, shed_tokens=1).messages[2].parts[0]
        assert (part.id, part.error, part.metadata) == ("call_0", True, {"match_count": 3})
        assert _is_placeholder(part.output)

    def test_calls_users_and_system_are_untouched_and_the_input_is_not_mutated(self) -> None:
        system = Message(role="system", parts=[TextPart(text="s" * BIG)])
        msgs = [system, *_rounds(BIG, BIG, BIG)]
        snapshot = [m.model_dump() for m in msgs]
        out = prune_prompt(msgs, shed_tokens=10**9)
        assert [m.model_dump() for m in msgs] == snapshot
        assert [m.role for m in out.messages] == [m.role for m in msgs]
        assert out.messages[0] == system and out.messages[1] == msgs[1] and out.messages[2] == msgs[2]

    def test_a_multi_result_tool_message_keeps_its_other_parts(self) -> None:
        msgs = [_user(), _call("a"), Message(role="tool", parts=[
            ToolResultPart(id="a", output="x" * BIG), ToolResultPart(id="b", output="short"),
        ]), _call("c"), _result("c", "y" * BIG), _call("d"), _result("d", "z" * BIG)]
        first = prune_prompt(msgs, shed_tokens=1, keep_rounds=2).messages[2].parts
        assert _is_placeholder(first[0].output) and first[1].output == "short"


class TestSizing:
    def test_the_caller_sizes_results_and_that_changes_the_choice(self) -> None:
        """Two results of equal length: a caller that knows one is denser prunes it first."""
        msgs = [_user(), _call("c1"), _result("c1", "prose " * 5000), _call("c2"),
                _result("c2", "9f86d081884c7d65" * 1875), _call("c3"), _result("c3", "k"),
                _call("c4"), _result("c4", "k")]
        keys = result_keys(msgs)
        dense = {keys[(4, 0)]}

        def size(part: ToolResultPart) -> int:
            return len(part.output) // (1 if part.output.startswith("9f86") else 4)

        out = prune_prompt(msgs, shed_tokens=1, size=size, min_tokens=100)
        assert out.added.omitted == dense

    def test_what_is_shed_is_the_size_of_what_was_removed_not_a_ratio_of_what_is_left(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        out = prune_prompt(msgs, shed_tokens=1)
        removed = default_size(msgs[2].parts[0]) - default_size(out.messages[2].parts[0])
        assert out.shed_tokens == removed


class TestStickyAndPositionStable:
    def test_a_recorded_set_reproduces_the_same_prune_on_the_raw_messages(self) -> None:
        raw = _rounds(BIG, BIG, BIG, BIG)
        first = prune_prompt(raw, shed_tokens=10**9)
        again, shed = apply_prune_set(raw, first.prune_set)
        assert again == first.messages and shed == first.shed_tokens

    def test_passing_the_full_set_back_adds_nothing_so_it_is_idempotent(self) -> None:
        raw = _rounds(BIG, BIG, BIG, BIG)
        first = prune_prompt(raw, shed_tokens=10**9)
        second = prune_prompt(raw, sticky=first.prune_set, shed_tokens=10**9)
        assert second.messages == first.messages
        assert not second.added and second.applied == first.added

    def test_it_survives_a_json_round_trip(self) -> None:
        raw = _rounds(BIG, BIG, BIG)
        added = prune_prompt(raw, shed_tokens=10**9).added
        restored = PruneSet.from_payload(added.to_payload())
        assert restored == added
        assert apply_prune_set(raw, restored)[0] == apply_prune_set(raw, added)[0]

    def test_the_key_names_the_exact_output_so_a_changed_result_is_not_pruned(self) -> None:
        raw = _rounds(BIG, BIG, BIG)
        added = prune_prompt(raw, shed_tokens=10**9).added
        changed = _rounds(BIG, BIG, BIG)
        changed[2] = _result("call_0", "DIFFERENT" * 5000)
        out, _ = apply_prune_set(changed, added)
        assert out[2].parts[0].output == "DIFFERENT" * 5000

    def test_the_same_raw_call_id_and_output_in_two_rounds_is_told_apart_by_position(self) -> None:
        raw = [_user(), _call("call_0"), _result("call_0", "X" * BIG),
               _call("call_0"), _result("call_0", "X" * BIG)]
        keys = result_keys(raw)
        assert keys[(2, 0)].endswith("#0") and keys[(4, 0)].endswith("#1")
        assert keys[(2, 0)].split("#")[0] == keys[(4, 0)].split("#")[0]

    def test_a_fresh_identical_result_in_the_newest_round_is_not_re_pruned(self) -> None:
        """The loop this guards against: Ollama and Gemini mint call_0 per stream.
        An old call_0 -> X was pruned; the model, told to call again, gets the
        identical X back. A recorded prune must not land on the fresh one."""
        old = [_user(), _call("call_0"), _result("call_0", "X" * BIG),
               _call("call_0"), _result("call_0", "Y" * BIG),
               _call("call_0"), _result("call_0", "Z" * BIG)]
        recorded = prune_prompt(old, shed_tokens=1).prune_set
        assert recorded.omitted, "the old X was pruned"
        later = [*old, _call("call_0"), _result("call_0", "X" * BIG)]  # the fresh repeat, newest
        out, _ = apply_prune_set(later, recorded)
        outs = _outputs(out)
        assert _is_placeholder(outs[0]), "the old X stays pruned"
        assert outs[-1] == "X" * BIG, "the fresh identical X is NOT pruned"

    def test_position_alone_protects_a_fresh_identical_result_outside_the_newest_rounds(self) -> None:
        """The newest-round rule is not what saves the fresh repeat here: two more
        rounds follow it, so it is OUTSIDE the newest two. Only the occurrence index
        tells it from the pruned original."""
        old = [_user(), _call("call_0"), _result("call_0", "X" * BIG),
               _call("call_0"), _result("call_0", "Y" * BIG),
               _call("call_0"), _result("call_0", "Z" * BIG)]
        recorded = prune_prompt(old, shed_tokens=1).prune_set
        later = [*old, _call("call_0"), _result("call_0", "X" * BIG),
                 _call("call_0"), _result("call_0", "W" * BIG),
                 _call("call_0"), _result("call_0", "V" * BIG)]
        outs = _outputs(apply_prune_set(later, recorded)[0])
        assert _is_placeholder(outs[0]), "the original X stays pruned"
        assert outs[3] == "X" * BIG, "the fresh X, outside the newest rounds, is not pruned"

    def test_a_recorded_omit_never_lands_on_a_result_in_the_newest_rounds(self) -> None:
        raw = _rounds(BIG, BIG, BIG)
        keys = result_keys(raw)
        sticky = PruneSet(omitted=frozenset({keys[(6, 0)], keys[(4, 0)]}))  # newest two rounds
        out = prune_prompt(raw, sticky=sticky, keep_rounds=2)
        assert _outputs(out.messages) == _outputs(raw)
        assert not out.applied

    def test_a_recorded_truncation_still_applies_once_its_round_is_no_longer_the_newest(self) -> None:
        """Otherwise the raw result returns on the very next call and the prompt
        flaps between reduced and raw."""
        raw = [_user(), _call("c1"), _result("c1", "x" * BIG)]
        forced = prune_prompt(raw, shed_tokens=1, force=True, truncate_chars=4_000)
        assert forced.added.truncated
        later = [*raw, _call("c2"), _result("c2", "short"), _call("c3"), _result("c3", "short")]
        out = prune_prompt(later, sticky=forced.prune_set)
        assert len(_outputs(out.messages)[0]) < 4_200

    def test_an_empty_set_changes_nothing(self) -> None:
        raw = _rounds(BIG, BIG)
        out, shed = apply_prune_set(raw, PruneSet())
        assert out == raw and shed == 0

    def test_a_union_keeps_both_sets(self) -> None:
        a = PruneSet(omitted=frozenset({"x:1#0"}))
        b = PruneSet(truncated={"y:2#0": 100})
        assert a.union(b) == PruneSet(omitted=frozenset({"x:1#0"}), truncated={"y:2#0": 100})

    def test_shed_by_sticky_is_reported_separately_from_the_new_shedding(self) -> None:
        raw = _rounds(BIG, BIG, BIG, BIG, BIG)
        first = prune_prompt(raw, shed_tokens=1)
        out = prune_prompt(raw, sticky=first.prune_set, shed_tokens=default_size(raw[4].parts[0]) - 100)
        assert out.shed_by_sticky == first.shed_tokens
        assert out.shed_tokens > out.shed_by_sticky and out.added.omitted


class TestAlreadyReducedComesFromTheSetNotTheText:
    def test_a_raw_output_that_spells_the_placeholder_is_an_ordinary_output(self) -> None:
        sneaky = "[output of 12 chars omitted to fit the context window; call the tool again] " + "q" * BIG
        raw = [_user(), _call("a"), _result("a", sneaky), _call("b"), _result("b", "y"),
               _call("c"), _result("c", "z")]
        out = prune_prompt(raw, shed_tokens=1)
        assert out.added.omitted, "text that merely looks reduced is still prunable"
        assert _outputs(out.messages)[0].startswith("[output of 40")

    def test_a_raw_output_that_spells_the_cut_marker_is_prunable_too(self) -> None:
        sneaky = "[... 5 chars omitted to fit the context window ...]" + "q" * BIG
        raw = [_user(), _call("a"), _result("a", sneaky), _call("b"), _result("b", "y"),
               _call("c"), _result("c", "z")]
        assert prune_prompt(raw, shed_tokens=1).added.omitted

    def test_results_the_sticky_set_reduced_are_not_chosen_again(self) -> None:
        raw = _rounds(BIG, BIG, BIG, BIG)
        first = prune_prompt(raw, shed_tokens=1)  # reduces round 0 only
        keys = result_keys(raw)
        more = prune_prompt(raw, sticky=first.prune_set, shed_tokens=default_size(raw[2].parts[0]) - 100)
        assert keys[(2, 0)] in first.added.omitted
        assert keys[(2, 0)] not in more.added.omitted, "phase 1 skips what the set already reduced"
        assert keys[(4, 0)] in more.added.omitted


class TestForce:
    def test_force_truncates_the_newest_round_when_placeholders_are_not_enough(self) -> None:
        msgs = [_user(), _call("c1"), _result("c1", "x" * BIG)]  # one round, protected
        assert prune_prompt(msgs, shed_tokens=5_000).shed_tokens == 0, "newest round is never replaced"
        forced = prune_prompt(msgs, shed_tokens=5_000, force=True, truncate_chars=4_000)
        text = _outputs(forced.messages)[0]
        assert len(text) < 4_200 and text.startswith("x") and text.endswith("x")
        assert forced.shed_tokens > 5_000 and forced.added.truncated

    def test_truncation_keeps_head_and_tail(self) -> None:
        body = "HEAD" + "m" * BIG + "TAIL"
        msgs = [_user(), _call("c1"), _result("c1", body)]
        text = _outputs(prune_prompt(msgs, shed_tokens=1, force=True, truncate_chars=3_000).messages)[0]
        assert text.startswith("HEAD") and text.endswith("TAIL")

    @pytest.mark.parametrize("keep", [3_000, 4_001, 8_000])
    def test_the_cut_marker_states_exactly_how_many_chars_were_removed(self, keep: int) -> None:
        body = "".join(chr(97 + i % 26) for i in range(BIG))
        msgs = [_user(), _call("c1"), _result("c1", body)]
        text = _outputs(prune_prompt(msgs, shed_tokens=1, force=True, truncate_chars=keep).messages)[0]
        marker = re.search(r"\[\.\.\. (\d+) chars omitted to fit the context window \.\.\.\]", text)
        assert marker, text[:80]
        head, tail = text.split(marker.group(0))
        head = head.rstrip("\n")
        tail = tail.lstrip("\n")
        assert int(marker.group(1)) == len(body) - len(head) - len(tail)
        assert body.startswith(head) and body.endswith(tail)
        assert len(head) + len(tail) == keep

    def test_a_truncation_is_sticky_too(self) -> None:
        msgs = [_user(), _call("c1"), _result("c1", "x" * BIG)]
        forced = prune_prompt(msgs, shed_tokens=1, force=True, truncate_chars=4_000)
        again, _ = apply_prune_set(msgs, forced.prune_set)
        assert again == forced.messages

    def test_force_prefers_placeholders_to_truncation(self) -> None:
        out = prune_prompt(_rounds(BIG, BIG, BIG), shed_tokens=1, force=True)
        assert out.added.omitted and not out.added.truncated

    def test_force_cannot_conjure_a_saving_there_is_none_to_make(self) -> None:
        msgs = [_user(), _call("c1"), _result("c1", "tiny")]
        out = prune_prompt(msgs, shed_tokens=10_000, force=True)
        assert out.shed_tokens == 0 and out.messages == msgs
