"""Today's compaction rules, characterised BEFORE the prompt-budget work touches them.

These tests pin what ``CompactionStrategy`` does now, including behaviour that is a limitation
rather than a design (marked as such): the budget work replaces the trigger's signal and moves
tier 1 off the persisted history, and a change to the rules below must be a decision, not an
accident. Each docstring says what is being pinned and why it matters to that work.
"""

from __future__ import annotations

import inspect
import json
import math
from types import SimpleNamespace

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.tail import tail_split
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    DocumentPart, Done, ImagePart, Message, TextDelta, TextPart, ToolCallPart, ToolResultPart,
)
from primer.model.media_tokens import DOCUMENT_TOKENS, IMAGE_TOKENS
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

PER_OUTPUT = CompactionStrategy.DEFAULT_PRUNE_PER_OUTPUT      # 20_000 tokens
TOTAL = CompactionStrategy.DEFAULT_PRUNE_TOTAL_THRESHOLD      # 40_000 tokens


def _model(context_length: int = 100_000) -> ResolvedModel:
    return ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m",
        context_length=context_length, config=ModelProfileConfig(),
    )


def _agent() -> Agent:
    return Agent(id="a", description="a", model=AgentModel(profile_id="p--m"), system_prompt=[])


def _result(call_id: str, tokens: int, fill: str = "x") -> Message:
    """A tool-result message whose heuristic size is exactly ``tokens`` (20 + ceil(len / 4))."""
    return Message(role="tool", parts=[ToolResultPart(id=call_id, output=fill * ((tokens - 20) * 4))])


def _call(call_id: str) -> Message:
    return Message(role="assistant", parts=[ToolCallPart(id=call_id, name="exec", arguments={"command": "ls"})])


def _history(*result_tokens: int) -> list[Message]:
    out: list[Message] = []
    for i, tokens in enumerate(result_tokens):
        out += [_call(f"c{i}"), _result(f"c{i}", tokens, fill=chr(ord("a") + i))]
    return out


def _outputs(history: list[Message]) -> list[str]:
    return [p.output for m in history for p in m.parts if isinstance(p, ToolResultPart)]


def _is_placeholder(text: str) -> bool:
    return text.startswith("[output of ") and text.endswith("the full text]")


class _NoLLM:
    """Fails if anything tries to summarise."""

    def stream(self, **_kwargs):
        raise AssertionError("the summariser was called")


class _SummaryLLM:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, **_kwargs):
        self.calls += 1

        async def _g():
            yield TextDelta(text="THE SUMMARY", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
        return _g()


class TestTheHeuristic:
    """The estimate the trigger compares with its budget. The new signal replaces it."""

    def test_text_is_a_quarter_of_its_characters_rounded_up_plus_8_per_message(self) -> None:
        estimate = CompactionStrategy._estimate_tokens
        assert estimate([Message(role="user", parts=[TextPart(text="x" * 400)])]) == 8 + 100
        assert estimate([Message(role="user", parts=[TextPart(text="x" * 401)])]) == 8 + 101

    def test_a_tool_call_is_50_plus_the_name_plus_a_quarter_of_its_json_arguments(self) -> None:
        arguments = {"command": "echo hello", "description": "d"}
        expected = 8 + 50 + len("exec") + math.ceil(len(json.dumps(arguments, ensure_ascii=False)) / 4)
        assert CompactionStrategy._estimate_tokens([
            Message(role="assistant", parts=[ToolCallPart(id="c", name="exec", arguments=arguments)])
        ]) == expected

    def test_a_tool_result_is_20_plus_a_quarter_of_its_output(self) -> None:
        assert CompactionStrategy._estimate_tokens([_result("c", 1_000)]) == 8 + 1_000

    def test_media_uses_the_flat_constants(self) -> None:
        image = Message(role="user", parts=[ImagePart(mime_type="image/png", data=b"\x00")])
        document = Message(role="user", parts=[DocumentPart(mime_type="application/pdf", data=b"\x00")])
        assert CompactionStrategy._estimate_tokens([image]) == 8 + IMAGE_TOKENS
        assert CompactionStrategy._estimate_tokens([document]) == 8 + DOCUMENT_TOKENS

    def test_it_measures_messages_only_never_the_system_prompt_or_the_tool_schemas(self) -> None:
        """LIMITATION pinned: the fixed part of a prompt (system prompt + tool schemas, about 3k tokens for a
        workspace agent) is invisible to the trigger, so a small-context model overflows while 'under' it."""
        assert list(inspect.signature(CompactionStrategy._estimate_tokens).parameters) == ["messages"]
        assert "tools" not in inspect.signature(CompactionStrategy.maybe_compact).parameters


class TestTheBudget:
    @pytest.mark.parametrize(
        ("context_length", "trigger"),
        [(4_096, 1_843), (8_192, 3_686), (16_384, 7_372), (32_768, 22_118), (128_000, 107_827), (200_000, 172_627)],
    )
    def test_the_trigger_is_90_percent_of_the_context_less_the_reserved_output(self, context_length, trigger) -> None:
        strategy = CompactionStrategy()
        budget = strategy._effective_budget(_model(context_length))
        assert int(strategy.trigger_ratio * budget) == trigger

    def test_the_reserved_output_is_clamped_to_half_the_context(self) -> None:
        """Without the clamp an 8192-context model would get a budget of 0 and compact on every turn."""
        strategy = CompactionStrategy()
        assert strategy._effective_budget(_model(8_192)) == 8_192 - 4_096
        assert strategy._effective_budget(_model(16_384)) == 16_384 - 8_192
        assert strategy._effective_budget(_model(16_385)) == 16_385 - 8_192  # the clamp stops binding here
        assert strategy._effective_budget(_model(2)) == 1
        assert strategy._effective_budget(_model(1)) == 0  # the one place the trigger still collapses to 0


class TestTierOneRule:
    """``_prune_tool_outputs``: the rule behind today's tier 1, with both inclusive boundaries."""

    @staticmethod
    def _prune(history):
        return CompactionStrategy._prune_tool_outputs(
            history, per_output_threshold=PER_OUTPUT, total_threshold=TOTAL,
        )

    def test_a_total_at_the_threshold_prunes_nothing_even_when_one_result_is_over_the_per_output_limit(self) -> None:
        history = _history(PER_OUTPUT + 5_000, TOTAL - (PER_OUTPUT + 5_000))  # exactly TOTAL tokens
        pruned, count = self._prune(history)
        assert count == 0 and pruned == history

    def test_one_token_over_the_total_prunes_every_result_over_the_per_output_limit(self) -> None:
        history = _history(PER_OUTPUT + 5_000, TOTAL - (PER_OUTPUT + 5_000) + 1)  # TOTAL + 1 tokens
        pruned, count = self._prune(history)
        assert count == 1
        assert _is_placeholder(_outputs(pruned)[0]) and _outputs(pruned)[1] == _outputs(history)[1]

    def test_only_results_strictly_over_the_per_output_limit_are_pruned_and_nothing_protects_the_newest(self) -> None:
        history = _history(30_000, 5_000, 21_000, PER_OUTPUT, PER_OUTPUT + 1)  # total far over TOTAL
        pruned, count = self._prune(history)
        flags = [_is_placeholder(o) for o in _outputs(pruned)]
        assert flags == [True, False, True, False, True], "at the limit is kept, one over is pruned, newest included"
        assert count == 3

    def test_the_placeholder_names_the_length_and_the_result_keeps_its_id_and_error(self) -> None:
        history = _history(PER_OUTPUT + 1_000, 30_000)
        history[1] = Message(role="tool", parts=[ToolResultPart(
            id="c0", output=history[1].parts[0].output, error=True,
        )])
        pruned, _ = self._prune(history)
        part = pruned[1].parts[0]
        length = len(history[1].parts[0].output)
        assert part.output == (
            f"[output of {length} chars omitted by compaction; check the persisted history if you need the full text]"
        )
        assert (part.id, part.error) == ("c0", True)
        assert pruned[0] is history[0], "messages without a pruned result are passed through untouched"

    def test_the_decision_depends_only_on_the_totals_now_so_it_flips_back_when_they_shrink(self) -> None:
        """LIMITATION pinned (N1): nothing is remembered. The same raw results are pruned while the total is
        over the threshold and sent whole once it is not, which is why a send-time record is needed."""
        big = _history(30_000, 30_000)
        assert self._prune(big)[1] == 2
        smaller = big[:2]  # the same first result alone: 30_000 tokens, under the 40_000 total
        assert self._prune(smaller)[1] == 0
        assert not _is_placeholder(_outputs(smaller)[0])


class TestWhatMaybeCompactDoes:
    @pytest.mark.asyncio
    async def test_below_the_trigger_it_does_nothing(self) -> None:
        result = await CompactionStrategy().maybe_compact(
            agent=_agent(), llm=_NoLLM(), model=_model(), history=_history(1_000), new_messages=[],  # type: ignore[arg-type]
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_when_pruning_is_enough_the_outcome_is_prune_only_and_the_summariser_is_not_called(self) -> None:
        history = _history(*[PER_OUTPUT + 2_500] * 4)  # 90k tokens of results, over the 82_627 trigger
        strategy = CompactionStrategy()
        result = await strategy.maybe_compact(
            agent=_agent(), llm=_NoLLM(), model=_model(), history=history, new_messages=[],  # type: ignore[arg-type]
        )
        assert result is not None
        assert result.summary_message is None and result.head_messages_replaced == 0
        assert result.pruned_tool_outputs == 4
        assert all(_is_placeholder(o) for o in _outputs(result.new_messages))
        assert result.estimated_tokens_before == CompactionStrategy._estimate_tokens(history)
        assert result.estimated_tokens_after == CompactionStrategy._estimate_tokens(result.new_messages)
        assert result.estimated_tokens_after < 82_627 <= result.estimated_tokens_before

    @pytest.mark.asyncio
    async def test_when_pruning_is_not_enough_it_summarises_and_the_kept_tail_can_leave_the_prompt_over_the_trigger(self) -> None:
        """LIMITATION pinned (F14b): the strategy never re-checks the result against the trigger. Big user text
        in the kept tail survives, so a compaction that fired can still hand the model an over-trigger prompt."""
        big = lambda c: Message(role="user", parts=[TextPart(text=c * 120_000)])  # noqa: E731  (30k tokens each)
        history: list[Message] = []
        for i in range(6):
            history += [big(chr(ord("A") + i)), Message(role="assistant", parts=[TextPart(text=f"reply {i}")])]
        llm = _SummaryLLM()
        result = await CompactionStrategy().maybe_compact(
            agent=_agent(), llm=llm, model=_model(), history=history, new_messages=[],  # type: ignore[arg-type]
        )
        assert result is not None and result.summary_message is not None and llm.calls == 1
        assert result.head_messages_replaced > 0
        assert result.estimated_tokens_after >= 82_627, "four 30k-token user turns in the tail keep it over the trigger"


class TestTheSilentNoOp:
    """LIMITATION pinned (F14a): tier 2 has nothing to summarise unless something precedes the ``tail_turns``-th most
    recent ASSISTANT message. With NO assistant message the head is everything (the opposite case); with fewer than
    ``tail_turns`` it is empty; with exactly that many it is empty only when the oldest kept one is the first message."""

    @staticmethod
    def _turns(assistants: int, *, starts_with_assistant: bool = False) -> list[Message]:
        messages: list[Message] = []
        for i in range(assistants):
            if not (starts_with_assistant and i == 0):
                messages.append(Message(role="user", parts=[TextPart(text=f"q{i}")]))
            messages.append(Message(role="assistant", parts=[TextPart(text=f"a{i}")]))
        return messages

    @pytest.mark.parametrize(
        ("assistants", "starts_with_assistant", "head_len", "tail_len"),
        [
            (0, False, 1, 0),   # no assistant message at all: the head is EVERYTHING (summarise it all)
            (1, False, 0, 2),   # fewer than tail_turns: nothing to summarise
            (3, False, 0, 6),
            (4, False, 1, 7),   # exactly tail_turns: the head is only what precedes the oldest kept assistant
            (4, True, 0, 7),    # ...and that is nothing when the oldest kept assistant is the FIRST message
            (5, False, 3, 7),
        ],
    )
    def test_when_the_head_is_empty_and_when_it_is_not(self, assistants, starts_with_assistant, head_len, tail_len) -> None:
        """The exact ``tail_split`` rule behind F14: a head exists only if something precedes the tail_turns-th most
        recent ASSISTANT message. A post-marker history starts with the summary, which is an assistant message, so
        there exactly ``tail_turns`` assistant messages is also a no-op."""
        messages = self._turns(assistants, starts_with_assistant=starts_with_assistant) if assistants else [
            Message(role="user", parts=[TextPart(text="only user text")])
        ]
        head, tail = tail_split(messages, tail_turns=4)
        assert (len(head), len(tail)) == (head_len, tail_len)

    @pytest.mark.asyncio
    async def test_a_history_far_over_the_trigger_but_with_few_assistant_messages_is_returned_unchanged(self) -> None:
        history: list[Message] = []
        for i in range(5):
            history.append(Message(role="user", parts=[TextPart(text=chr(ord("A") + i) * 120_000)]))  # 150k tokens
        history.insert(2, Message(role="assistant", parts=[TextPart(text="one reply")]))
        result = await CompactionStrategy().maybe_compact(
            agent=_agent(), llm=_NoLLM(), model=_model(), history=history, new_messages=[],  # type: ignore[arg-type]
        )
        assert result is not None, "the trigger fired"
        assert result.summary_message is None and result.head_messages_replaced == 0
        assert result.new_messages == history, "nothing was shortened"
        assert result.estimated_tokens_after >= 82_627, "and the oversized prompt goes out as it is"
