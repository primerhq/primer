"""primer.agent.prune: sticky, caller-sized, envelope-preserving tool-result pruning."""

from __future__ import annotations

import pytest

from primer.agent.prune import (
    PruneSet,
    apply_prune_set,
    default_size,
    prune_key,
    prune_to_shed,
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


class TestShedding:
    def test_replaces_the_largest_old_result_first_and_stops_when_enough_is_shed(self) -> None:
        msgs = _rounds(BIG, BIG * 2, BIG, BIG)  # four rounds; the newest two are kept
        out = prune_to_shed(msgs, shed_tokens=default_size(msgs[4].parts[0]) - 100)
        outs = _outputs(out.messages)
        assert outs[1].startswith("[output of 80000 chars omitted"), "the biggest old result goes first"
        assert outs[0] == "a" * BIG, "enough was shed after one: no over-pruning"
        assert outs[2:] == ["c" * BIG, "d" * BIG]

    def test_the_newest_rounds_are_never_replaced(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        out = prune_to_shed(msgs, shed_tokens=10**9, keep_rounds=2)
        outs = _outputs(out.messages)
        assert outs[0].startswith("[output of")
        assert outs[1:] == ["b" * BIG, "c" * BIG]

    def test_results_under_the_minimum_are_left_alone(self) -> None:
        msgs = _rounds(400, 400, BIG, BIG)
        out = prune_to_shed(msgs, shed_tokens=10**9, min_tokens=1_000)
        assert _outputs(out.messages)[:2] == ["a" * 400, "b" * 400]

    def test_it_reports_what_it_shed_and_what_it_added(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        out = prune_to_shed(msgs, shed_tokens=1)
        assert out.shed_tokens == sum(default_size(p) for m in msgs for p in m.parts
                                      if isinstance(p, ToolResultPart)) - sum(
            default_size(p) for m in out.messages for p in m.parts if isinstance(p, ToolResultPart)
        )
        assert out.added.omitted == {prune_key(msgs[2].parts[0])}

    def test_nothing_to_shed_is_a_no_op(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        assert prune_to_shed(msgs, shed_tokens=0).messages == msgs
        assert not prune_to_shed(msgs, shed_tokens=0).added

    def test_it_is_idempotent(self) -> None:
        msgs = _rounds(BIG, BIG, BIG, BIG)
        once = prune_to_shed(msgs, shed_tokens=10**9)
        twice = prune_to_shed(once.messages, shed_tokens=10**9)
        assert twice.messages == once.messages and not twice.added and twice.shed_tokens == 0


class TestEnvelope:
    def test_only_output_text_changes(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        msgs[2] = Message(role="tool", parts=[ToolResultPart(
            id="call_0", output="z" * BIG, error=True, metadata={"match_count": 3},
        )])
        out = prune_to_shed(msgs, shed_tokens=1)
        part = out.messages[2].parts[0]
        assert (part.id, part.error, part.metadata) == ("call_0", True, {"match_count": 3})
        assert part.output.startswith("[output of")

    def test_calls_users_and_system_are_untouched_and_the_input_is_not_mutated(self) -> None:
        system = Message(role="system", parts=[TextPart(text="s" * BIG)])
        msgs = [system, *_rounds(BIG, BIG, BIG)]
        snapshot = [m.model_dump() for m in msgs]
        out = prune_to_shed(msgs, shed_tokens=10**9)
        assert [m.model_dump() for m in msgs] == snapshot
        assert [m.role for m in out.messages] == [m.role for m in msgs]
        assert out.messages[0] == system and out.messages[1] == msgs[1] and out.messages[2] == msgs[2]

    def test_a_multi_result_tool_message_keeps_its_other_parts(self) -> None:
        msgs = [_user(), _call("a"), Message(role="tool", parts=[
            ToolResultPart(id="a", output="x" * BIG), ToolResultPart(id="b", output="short"),
        ]), _call("c"), _result("c", "y" * BIG), _call("d"), _result("d", "z" * BIG)]
        out = prune_to_shed(msgs, shed_tokens=1, keep_rounds=2)
        first = out.messages[2].parts
        assert first[0].output.startswith("[output of") and first[1].output == "short"


class TestSizing:
    def test_the_caller_sizes_results_and_that_changes_the_choice(self) -> None:
        """Two results of equal length: a caller that knows one is denser prunes it first."""
        msgs = [_user(), _call("c1"), _result("c1", "prose " * 5000), _call("c2"),
                _result("c2", "9f86d081884c7d65" * 1875), _call("c3"), _result("c3", "k"),
                _call("c4"), _result("c4", "k")]
        dense = {prune_key(p) for m in msgs for p in m.parts
                 if isinstance(p, ToolResultPart) and p.output.startswith("9f86")}

        def size(part: ToolResultPart) -> int:
            return len(part.output) // (1 if prune_key(part) in dense else 4)

        out = prune_to_shed(msgs, shed_tokens=1, size=size, min_tokens=100)
        assert out.added.omitted == dense

    def test_a_dense_remainder_is_not_rescaled_by_a_ratio(self) -> None:
        """What is shed is the SIZE of what was removed, not a ratio times what is left."""
        msgs = _rounds(BIG, BIG, BIG)
        out = prune_to_shed(msgs, shed_tokens=1)
        removed = default_size(msgs[2].parts[0]) - default_size(out.messages[2].parts[0])
        assert out.shed_tokens == removed


class TestSticky:
    def test_a_recorded_set_reproduces_the_same_prune_on_the_raw_messages(self) -> None:
        raw = _rounds(BIG, BIG, BIG, BIG)
        first = prune_to_shed(raw, shed_tokens=10**9)
        again, shed = apply_prune_set(raw, first.added)
        assert again == first.messages
        assert shed == first.shed_tokens

    def test_it_survives_a_json_round_trip(self) -> None:
        raw = _rounds(BIG, BIG, BIG)
        added = prune_to_shed(raw, shed_tokens=10**9).added
        restored = PruneSet.from_payload(added.to_payload())
        assert restored == added
        assert apply_prune_set(raw, restored)[0] == apply_prune_set(raw, added)[0]

    def test_the_key_names_the_exact_output_so_a_changed_result_is_not_pruned(self) -> None:
        raw = _rounds(BIG, BIG, BIG)
        added = prune_to_shed(raw, shed_tokens=10**9).added
        changed = _rounds(BIG, BIG, BIG)
        changed[2] = _result("call_0", "DIFFERENT" * 5000)
        out, _ = apply_prune_set(changed, added)
        assert out[2].parts[0].output == "DIFFERENT" * 5000

    def test_the_same_raw_call_id_in_two_rounds_is_told_apart_by_content(self) -> None:
        raw = _rounds(BIG, BIG, BIG)  # every result id is 'call_0'
        keys = {prune_key(p) for m in raw for p in m.parts if isinstance(p, ToolResultPart)}
        assert len(keys) == 3

    def test_an_empty_set_changes_nothing(self) -> None:
        raw = _rounds(BIG, BIG)
        out, shed = apply_prune_set(raw, PruneSet())
        assert out == raw and shed == 0

    def test_a_union_keeps_both_sets(self) -> None:
        a = PruneSet(omitted=frozenset({"x:1"}))
        b = PruneSet(truncated={"y:2": 100})
        assert a.union(b) == PruneSet(omitted=frozenset({"x:1"}), truncated={"y:2": 100})


class TestForce:
    def test_force_truncates_the_newest_round_when_placeholders_are_not_enough(self) -> None:
        msgs = [_user(), _call("c1"), _result("c1", "x" * BIG)]  # one round, protected
        plain = prune_to_shed(msgs, shed_tokens=5_000)
        assert plain.shed_tokens == 0, "without force the newest round is never touched"
        forced = prune_to_shed(msgs, shed_tokens=5_000, force=True, truncate_chars=4_000)
        text = _outputs(forced.messages)[0]
        assert len(text) < 4_200
        assert text.startswith("x") and text.endswith("x")
        assert "chars omitted to fit the context window" in text
        assert forced.shed_tokens > 5_000 and forced.added.truncated

    def test_truncation_keeps_head_and_tail_and_marks_the_cut(self) -> None:
        body = "HEAD" + "m" * BIG + "TAIL"
        msgs = [_user(), _call("c1"), _result("c1", body)]
        text = _outputs(prune_to_shed(msgs, shed_tokens=1, force=True, truncate_chars=3_000).messages)[0]
        assert text.startswith("HEAD") and text.endswith("TAIL")

    def test_a_truncation_is_sticky_too(self) -> None:
        msgs = [_user(), _call("c1"), _result("c1", "x" * BIG)]
        forced = prune_to_shed(msgs, shed_tokens=1, force=True, truncate_chars=4_000)
        again, _ = apply_prune_set(msgs, forced.added)
        assert again == forced.messages

    def test_force_prefers_placeholders_to_truncation(self) -> None:
        msgs = _rounds(BIG, BIG, BIG)
        out = prune_to_shed(msgs, shed_tokens=1, force=True)
        assert out.added.omitted and not out.added.truncated

    def test_force_cannot_conjure_a_saving_there_is_none_to_make(self) -> None:
        msgs = [_user(), _call("c1"), _result("c1", "tiny")]
        out = prune_to_shed(msgs, shed_tokens=10_000, force=True)
        assert out.shed_tokens == 0 and out.messages == msgs


@pytest.mark.parametrize("keep_rounds", [0, 1, 3])
def test_keep_rounds_bounds_what_is_protected(keep_rounds: int) -> None:
    msgs = _rounds(BIG, BIG, BIG, BIG)
    out = prune_to_shed(msgs, shed_tokens=10**9, keep_rounds=keep_rounds)
    outs = _outputs(out.messages)
    reduced = [o.startswith("[output of") for o in outs]
    assert reduced == [i < 4 - keep_rounds for i in range(4)]
