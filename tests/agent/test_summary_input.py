"""``reduce_summary_input``: what the summariser reads, made to fit its own window (A.4, R8).

The order loses the least: shed tool results first (largest first, the envelope stays), then fold the rest into at
most four chunks (each call is sent the summary so far and the next chunk), then cut a single unit that is over a
chunk by itself. Pure, so the properties are pinned here and the model calls in ``test_summariser_overflow_recovery.py``.
"""

from __future__ import annotations

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.summary_input import (
    MAX_CHUNKS,
    PROVIDER_COUNTED_MORE,
    SUMMARISER_SAFETY,
    SummaryInputUnreachable,
    reduce_summary_input,
    size_summariser_input,
)
from primer.model.chat import ImagePart, Message, TextPart, ToolCallPart, ToolResultPart

SIZE = CompactionStrategy._estimate_tokens  # noqa: SLF001
PART = CompactionStrategy._estimate_part_tokens  # noqa: SLF001


def _text(role: str, chars: int, tag: str = "t") -> Message:
    return Message(role=role, parts=[TextPart(text=tag + "x" * chars)])


def _round(i: int, result_chars: int, *, args: dict | None = None) -> list[Message]:
    return [
        Message(role="assistant", parts=[ToolCallPart(id=f"c{i}", name="exec", arguments=args or {"cmd": "ls"})]),
        Message(role="tool", parts=[ToolResultPart(id=f"c{i}", output=chr(97 + i) * result_chars)]),
    ]


def _reduce(head, *, goal: int, chunk: int, **kwargs):
    return reduce_summary_input(head, size=SIZE, part_size=PART, goal_tokens=goal, chunk_tokens=chunk, **kwargs)


def _outputs(messages) -> list[str]:
    return [p.output for m in messages for p in m.parts if isinstance(p, ToolResultPart)]


class TestSheddingResults:
    def test_the_largest_results_go_first_and_only_as_far_as_needed(self) -> None:
        head = [_text("user", 40), *_round(0, 8_000), *_round(1, 80_000), *_round(2, 40_000), _text("assistant", 40)]
        out = _reduce(head, goal=SIZE(head) - 15_000, chunk=10**9)    # the 20k-token result alone is enough
        assert len(out.chunks) == 1 and out.report.pruned == 1 and out.report.folded_chunks == 0
        outputs = _outputs(out.chunks[0])
        assert "left out of the summariser's input" in outputs[1], "the biggest (round 1) was the one omitted"
        assert outputs[0] == "a" * 8_000 and outputs[2] == "c" * 40_000, "the others are untouched"

    def test_older_goes_first_among_equals(self) -> None:
        head = [*_round(0, 40_000), *_round(1, 40_000)]
        out = _reduce(head, goal=SIZE(head) - 8_000, chunk=10**9)
        outputs = _outputs(out.chunks[0])
        assert "left out" in outputs[0] and outputs[1] == "b" * 40_000

    def test_the_envelope_stays_so_no_call_loses_its_result(self) -> None:
        head = [*_round(0, 80_000), *_round(1, 80_000), *_round(2, 80_000)]
        out = _reduce(head, goal=2_000, chunk=10**9)
        flat = [m for chunk in out.chunks for m in chunk]
        calls = {p.id for m in flat for p in m.parts if isinstance(p, ToolCallPart)}
        results = {p.id for m in flat for p in m.parts if isinstance(p, ToolResultPart)}
        assert calls == results == {"c0", "c1", "c2"}

    def test_a_small_result_is_not_worth_omitting(self) -> None:
        head = [_text("user", 40), *_round(0, 200), *_round(1, 200)]       # 50 tokens each: under the floor
        out = _reduce(head, goal=SIZE(head) - 50, chunk=10**9)
        assert out.report.pruned == 0, "nothing was omitted: they are under the floor, so the fold is what is left"
        assert _outputs([m for c in out.chunks for m in c]) == ["a" * 200, "b" * 200]

    def test_nothing_is_done_to_a_head_that_already_fits(self) -> None:
        head = [_text("user", 400), *_round(0, 400)]
        out = _reduce(head, goal=10**9, chunk=10**9)
        assert out.chunks == [head] and out.report.as_payload() == {"pruned": 0, "folded_chunks": 0, "truncated_parts": 0}


class TestTheRollingFold:
    def test_what_shedding_cannot_fix_is_folded_in_chunks_that_fit(self) -> None:
        head = [m for i in range(6) for m in (_text("user", 12_000, f"q{i}"), _text("assistant", 12_000, f"a{i}"))]
        out = _reduce(head, goal=8_000, chunk=10_000)               # 3k tokens a message: three to a chunk
        assert out.report.folded_chunks == len(out.chunks) == 4 and out.report.pruned == 0
        assert all(SIZE(chunk) <= 10_000 for chunk in out.chunks), "every call fits"
        assert [m for chunk in out.chunks for m in chunk] == head, "nothing dropped, nothing reordered"

    def test_a_call_is_never_separated_from_its_results_by_a_chunk_boundary(self) -> None:
        head = [m for i in range(5) for m in (_text("user", 8_000, f"q{i}"), *_round(i, 200, args={"cmd": "x" * 8_000}))]
        out = _reduce(head, goal=6_000, chunk=14_000, max_chunks=8)
        for chunk in out.chunks:
            calls = {p.id for m in chunk for p in m.parts if isinstance(p, ToolCallPart)}
            results = {p.id for m in chunk for p in m.parts if isinstance(p, ToolResultPart)}
            assert calls == results

    def test_a_head_that_needs_more_than_the_bound_is_unreachable(self) -> None:
        head = [_text("user", 12_000, f"q{i}") for i in range(12)]
        with pytest.raises(SummaryInputUnreachable, match=f"bounded at {MAX_CHUNKS}"):
            _reduce(head, goal=4_000, chunk=6_000)


class TestCuttingASingleUnit:
    def test_a_message_that_alone_is_over_a_chunk_is_cut_head_and_tail(self) -> None:
        head = [_text("user", 400_000, "HEAD-"), _text("assistant", 400, "after")]
        out = _reduce(head, goal=8_000, chunk=10_000)
        assert out.report.truncated_parts == 1
        cut = out.chunks[0][0].parts[0].text
        assert cut.startswith("HEAD-") and "left out of the summariser's input" in cut and cut.endswith("x")
        assert all(SIZE(chunk) <= 10_000 for chunk in out.chunks)

    def test_a_cut_that_leaves_one_chunk_is_one_call_not_a_fold(self) -> None:
        out = _reduce([_text("user", 400_000, "HEAD-")], goal=8_000, chunk=10_000)
        assert len(out.chunks) == 1 and out.report.truncated_parts == 1
        assert out.report.folded_chunks == 0

    def test_a_long_string_in_a_tool_calls_arguments_is_cut_and_the_call_survives(self) -> None:
        head = [*_round(0, 200, args={"path": "a.txt", "content": "z" * 200_000})]
        out = _reduce(head, goal=8_000, chunk=10_000)
        call = out.chunks[0][0].parts[0]
        assert isinstance(call, ToolCallPart) and call.arguments["path"] == "a.txt", "the other arguments are intact"
        assert len(call.arguments["content"]) < 60_000 and out.report.truncated_parts >= 1

    def test_media_is_replaced_by_a_placeholder(self) -> None:
        image = Message(role="user", parts=[ImagePart(url="https://example.test/a.png"), TextPart(text="what is this?")])
        out = _reduce([image], goal=600, chunk=700)                      # an image is a flat ~1,000 tokens: over a chunk
        first = [p for m in out.chunks[0] for p in m.parts]
        assert not any(isinstance(p, ImagePart) for p in first)
        assert any(isinstance(p, TextPart) and "image left out" in p.text for p in first)

    def test_a_unit_that_cannot_be_cut_small_enough_is_unreachable(self) -> None:
        head = [_text("user", 400, "q")] + [
            Message(role="user", parts=[TextPart(text="y" * 150) for _ in range(40)]),     # 40 parts, none shrinkable
        ]
        with pytest.raises(SummaryInputUnreachable):
            _reduce(head, goal=100, chunk=300)

    def test_the_input_is_not_modified(self) -> None:
        head = [_text("user", 100_000, "keep-"), *_round(0, 90_000)]
        before = [m.model_dump() for m in head]
        _reduce(head, goal=5_000, chunk=6_000, max_chunks=8)
        assert [m.model_dump() for m in head] == before


def _size(**kw):
    return size_summariser_input(**{"window": 100_000, "budget": 91_808, "summary_tokens": 4_096, "frame": 200, "current": 150_000, **kw})


class TestHowMuchTheRetryMayRead:
    def test_a_head_our_estimate_says_does_not_fit_is_reduced_to_a_share_of_the_room(self) -> None:
        sizing = _size()
        room = 100_000 - 4_096 - 200
        assert sizing.goal == int(SUMMARISER_SAFETY * room), "the window less THIS call's own output, not the turn's reserve"

    def test_a_head_our_estimate_says_fits_is_reduced_anyway_because_the_provider_counted_more(self) -> None:
        sizing = _size(current=30_000)
        assert sizing.goal == int(PROVIDER_COUNTED_MORE * 30_000)

    def test_a_chunk_is_never_the_size_of_the_head_that_was_just_rejected(self) -> None:
        """The head is under a chunk (45.9k at this budget) and still overflowed: a chunk of its own size is the same input."""
        sizing = _size(current=30_000)
        assert sizing.chunk_tokens <= sizing.goal < 30_000

    def test_a_chunk_is_half_the_budget_when_the_window_is_roomy(self) -> None:
        sizing = _size()
        assert (sizing.chunk_tokens, sizing.max_chunks) == (int(0.5 * 91_808), MAX_CHUNKS)

    def test_a_fold_call_leaves_room_for_the_summary_so_far(self) -> None:
        """Every call after the first carries the summary (up to its allowance) beside its chunk: on a small window
        that, not the half of the budget, bounds the chunk."""
        sizing = _size(window=12_000, budget=6_000, current=50_000)
        target = int(SUMMARISER_SAFETY * (12_000 - 4_096 - 200))
        assert sizing.chunk_tokens == target - 4_096 < int(0.5 * 6_000)
        assert sizing.chunk_tokens + 4_096 <= target, "chunk + the summary so far fits the target"

    def test_when_no_chunk_fits_beside_the_summary_the_retry_is_one_call(self) -> None:
        """An 8k model: the window leaves room for a single call but not for a fold (the old guard refused it)."""
        sizing = _size(window=8_192, budget=4_096, current=15_000)
        target = int(SUMMARISER_SAFETY * (8_192 - 4_096 - 200))
        assert target > 0 and target - 4_096 < 0 < sizing.chunk_tokens
        assert (sizing.max_chunks, sizing.chunk_tokens) == (1, sizing.goal) and sizing.goal <= target

    def test_a_window_with_no_room_for_any_input_is_unreachable(self) -> None:
        with pytest.raises(SummaryInputUnreachable, match="no room"):
            _size(window=4_200)                                           # less than its own output and prompt leave

    def test_a_head_just_under_the_target_is_cut_by_the_provider_margin_not_sent_at_97_percent(self) -> None:
        """Our estimate says the head fits (it is under the ROOM), so the provider counted more: the cut is the 0.6
        margin however close to the target it is. Branching on the target instead sends a head 2% under it, which
        any provider counting 1.25x more rejects again."""
        room = 100_000 - 4_096 - 200
        for current in (int(0.78 * room), int(0.85 * room), room):
            assert _size(current=current).goal == int(PROVIDER_COUNTED_MORE * current), current
        assert _size(current=room + 1).goal == int(SUMMARISER_SAFETY * room), "over the room: our own count explains it"

    def test_an_overflow_the_tool_schemas_explain_does_not_cut_the_head(self) -> None:
        """A tool-enabled call carries the catalogue; the text-only retry does not. When the head alone fits and the
        head plus the schemas did not, the retry is the unchanged head: cutting it by the margin is a cut for nothing."""
        kw = {"window": 64_000, "budget": 55_000, "current": 15_500}
        explained = _size(**kw, first_call_extra=50_000)
        assert explained.goal == int(SUMMARISER_SAFETY * (64_000 - 4_096 - 200)) > 15_500
        assert _size(**kw).goal == int(PROVIDER_COUNTED_MORE * 15_500), "without schemas the same head was a provider miscount"

    def test_a_head_an_earlier_tool_round_was_accepted_with_is_never_cut_below_its_own_size(self) -> None:
        """Round one of a tool loop carried the head and the schemas and the provider took it: the head alone, text only,
        fits, whatever our count says. Both branches keep it whole."""
        over_the_target = _size(current=90_000, head_known_to_fit=True)
        assert over_the_target.goal == 90_000, "our count had it over the target, the provider's acceptance says otherwise"
        assert _size(current=90_000).goal < 90_000, "(without the evidence it is cut)"
        under = _size(current=30_000, head_known_to_fit=True)
        assert under.goal == 30_000, "and the provider-counted-more cut does not apply to a head that was accepted"

