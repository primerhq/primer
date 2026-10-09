"""Two-tier history compaction for the agent executors.

The :class:`CompactionStrategy` is shared between
:class:`primer.agent.AgentExecutor` (chat threads) and
:class:`primer.agent.WorkspaceAgentExecutor` (workspace-backed). It is
called between turns to keep the prompt under the configured LLM's
context limit.

Two tiers:

1. **Pruning (cheap)** -- replace oversized tool-result outputs with
   placeholder text in-place. The call/result envelope is preserved
   so the LLM doesn't see orphaned tool calls.
2. **Full compaction (expensive)** -- replace the head of the history
   with one assistant-role summary message produced by calling the
   same LLM with the agent's :attr:`Agent.compaction_prompt` (or the
   system default). The tail is kept verbatim: it is bounded by size, it
   never splits a tool call from its results, and it always contains the
   input the model has not answered yet (see :func:`split_for_compaction`).
   When nothing can be summarised, or the prompt is still over the trigger
   afterwards, the result says so (``CompactedTurn.unreducible``).

Token counting uses a conservative character heuristic (``_estimate_tokens``).

See ``docs/superpowers/specs/2026-05-03-agent-executor-design.md`` for
the surrounding design and ``research/compaction.md`` for the
empirical justification of the approach (especially "tool exchanges
can be dropped if replaced by a prose summary").
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from primer.agent.prompts import DEFAULT_COMPACTION_PROMPT
from primer.agent.overflow import ReplayGuard
from primer.agent.summary_input import (
    SummaryInputReduction,
    SummaryInputUnreachable,
    reduce_summary_input,
    size_summariser_input,
)
from primer.agent.tail import CompactionSplit, split_for_compaction
from primer.common.context_overflow import is_context_overflow
from primer.common.log import redact_credentials
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import (
    CompactionSummary,
    Error,
    ExtendedEvent,
    Message,
    Part,
    StreamEvent,
    TextDelta,
    TextPart,
    Tool,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    _ExecutorToolResult,
    output_to_message,
)
from primer.model.except_ import ServerError, SummariserOverflow
from primer.model.media_tokens import media_tokens
from primer.observability import metrics as _metrics


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model_profile import ResolvedModel


class CompactionToolExecutor(Protocol):
    """The slice of ``ToolExecutionManager`` the compaction loop needs.

    Duck-typed so the strategy stays decoupled from the executor layer and
    is trivially fakeable in tests.
    """

    async def list_tools(self, *, principal: str | None = ...) -> list[Tool]: ...

    async def execute(
        self, call: ToolCallPart, *, principal: str | None = ...
    ) -> ToolResultPart: ...


# Fallback cap on the compaction tool loop when the agent sets no
# ``max_tool_turns`` -- keeps an ill-behaved compaction prompt from looping
# unbounded during an automatic, unattended step.
DEFAULT_COMPACTION_TOOL_TURNS = 8


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-model context-length fallback table.
# ---------------------------------------------------------------------------
#
# Used when the resolved :attr:`ResolvedModel.context_length` is unavailable
# (e.g. an out-of-band model name not registered with any provider). The
# table starts small -- the four shipped LLM adapters' commonly-used
# flagship models -- and grows as new models land.

DEFAULT_CONTEXT_LIMIT = 100_000

MODEL_CONTEXT_FALLBACK: dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o1": 200_000,
    "o3": 200_000,
    # Anthropic
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    # Google
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    # Ollama (varies wildly per model; conservative)
    "llama3.2": 128_000,
    "qwen2.5": 128_000,
}


def lookup_context_length(*, model_name: str, configured: int | None = None) -> int:
    """Return the model's context length, preferring the configured value.

    Resolution order:

    1. ``configured`` -- the :attr:`ResolvedModel.context_length` from the
       provider registry, if supplied.
    2. The hardcoded :data:`MODEL_CONTEXT_FALLBACK` entry for
       ``model_name``.
    3. :data:`DEFAULT_CONTEXT_LIMIT`.
    """
    if configured is not None and configured > 0:
        return configured
    return MODEL_CONTEXT_FALLBACK.get(model_name, DEFAULT_CONTEXT_LIMIT)


# ---------------------------------------------------------------------------
# CompactedTurn
# ---------------------------------------------------------------------------


class CompactedTurn(BaseModel):
    """Result of a :meth:`CompactionStrategy.maybe_compact` pass.

    Carries the new history (with the head replaced by a summary
    message), plus telemetry for logging / observability. The
    strategy is stateless; the caller takes this result and applies
    it to the persistent history via the executor's
    ``_replace_compacted_head`` hook.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    new_messages: list[Message] = Field(
        ...,
        description=(
            "The compacted history. Includes the new summary message "
            "in head position when full compaction ran."
        ),
    )
    summary_message: Message | None = Field(
        default=None,
        description=(
            "The new assistant-role message that replaces the "
            "compactable head. ``None`` when only output-pruning ran "
            "(no full summarisation was needed)."
        ),
    )
    pruned_tool_outputs: int = Field(
        default=0,
        ge=0,
        description=(
            "How many oversized tool outputs were trimmed during the "
            "pruning pass."
        ),
    )
    head_messages_replaced: int = Field(
        default=0,
        ge=0,
        description=(
            "How many head messages were folded into the summary "
            "(0 if pruning sufficed)."
        ),
    )
    estimated_tokens_before: int = Field(..., ge=0)
    estimated_tokens_after: int = Field(..., ge=0)
    unreducible: str | None = Field(
        default=None,
        description=(
            "Why the prompt could not be brought under the trigger. In every "
            "``unreducible`` or ``skipped`` case ``new_messages`` is the history "
            "unchanged, no summariser call was made and no marker is written. "
            "``unreducible``: ``empty_head`` (nothing precedes the part that may not "
            "be summarised), ``fixed_over_budget`` (the fixed part, system prompt plus "
            "tool schemas, alone fills the budget), ``protected_over_budget`` (the fixed "
            "part plus the protected input, the current turn's unanswered part and the "
            "newest unit, fills it). ``skipped`` (``cannot_reach_trigger``): even the "
            "smallest result, fixed + protected + the summary allowance, is at or over the "
            "trigger and the prompt (after the tier-1 prune) still fits the budget, so compacting "
            "would only repeat every turn; it is allowed again once the prompt no longer fits the "
            "budget. ``skipped`` (``recently_compacted``): the same, for a prompt that does not fit "
            "the budget but fits the window and has not grown by a summary allowance since the last "
            "compaction left it (summarising the summary would gain nothing). "
            "``over_trigger``: summarised (a marker IS written) and still over. "
            "``None`` when compaction did what it was asked."
        ),
    )
    outcome: str = Field(
        default="summarised",
        description=(
            "The labelled outcome counted by ``compaction_outcomes_total``: "
            "``pruned`` (tier 1 sufficed), ``summarised``, ``unreducible`` (no "
            "marker; a failure), ``skipped`` (no marker; deliberately not compacted: the "
            "trigger cannot be reached, and the prompt fits the budget or has not grown since "
            "the last compaction left it) or ``insufficient`` (summarised, still over the trigger)."
        ),
    )
    trigger_tokens: int | None = Field(
        default=None,
        description="The trigger this compaction was measured against, in estimated tokens.",
    )
    budget_tokens: int | None = Field(
        default=None,
        description=(
            "The window allowance the prompt was measured against (the context minus the reserved "
            "output), in estimated tokens: what ``estimated_tokens_after`` must stay under for the "
            "prompt to fit at all. ``None`` when the producer did not say."
        ),
    )
    summary_after: int = Field(
        default=0,
        ge=0,
        description=(
            "Where the summary sits in ``new_messages``: after this many kept messages. ``0`` "
            "(the usual) puts it in front; a turn whose early tool rounds were summarised puts it "
            "after the user run that opened the turn, so the question stays first and verbatim."
        ),
    )
    summary_input_reduced: dict[str, int] | None = Field(
        default=None,
        description=(
            "Set when the summariser's own call overflowed: made again, text only, on a reduced "
            "input (``pruned`` tool results left out, ``folded_chunks`` chunks of the rolling fold, 0 "
            "for one call, and ``truncated_parts`` parts cut head and tail or media replaced; all zero when "
            "the retry needed nothing taken out), or, for a tool-enabled summariser whose loop overflowed in "
            "a later round, ended with the summary it had written (``tool_loop_cut_round``, the other "
            "counts zero). The marker records it as ``summary_input_reduced``. ``None`` when no call "
            "overflowed."
        ),
    )
    fixed_overhead_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "The part of every prompt no history can give back (system prompt plus tool "
            "schemas), in estimated tokens. ``estimated_tokens_before`` / ``_after`` and the "
            "trigger comparison include it."
        ),
    )


# ---------------------------------------------------------------------------
# CompactionStrategy
# ---------------------------------------------------------------------------


class CompactionStrategy:
    """Two-tier history compactor: tool-output pruning, then full summary.

    Stateless w.r.t. the executor; receives the proposed prompt
    (history + new user messages) and returns a :class:`CompactedTurn`
    if compaction was needed, else :data:`None`.

    The strategy uses the agent's :attr:`Agent.compaction_prompt` if
    non-empty, else the system default in
    :data:`primer.agent.prompts.DEFAULT_COMPACTION_PROMPT`. Compaction
    summarisation calls back into the SAME ``llm`` / ``model`` the
    agent uses (cheaper than running a separate model per the
    user-confirmed design decision).
    """

    DEFAULT_TRIGGER_RATIO: float = 0.90
    DEFAULT_RESERVED_OUTPUT: int = 8192
    DEFAULT_TAIL_TURNS: int = 4
    DEFAULT_TAIL_BUDGET_FRACTION: float = 0.5
    DEFAULT_REDUCED_FRACTION: float = 0.6
    DEFAULT_PRUNE_PER_OUTPUT: int = 20_000
    DEFAULT_PRUNE_TOTAL_THRESHOLD: int = 40_000
    DEFAULT_SUMMARY_MAX_TOKENS: int = 4096

    def __init__(
        self,
        *,
        trigger_ratio: float = DEFAULT_TRIGGER_RATIO,
        reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT,
        tail_turns: int = DEFAULT_TAIL_TURNS,
        tail_budget_fraction: float = DEFAULT_TAIL_BUDGET_FRACTION,
        prune_per_output_tokens: int = DEFAULT_PRUNE_PER_OUTPUT,
        prune_total_threshold: int = DEFAULT_PRUNE_TOTAL_THRESHOLD,
        summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
    ) -> None:
        if not 0 < trigger_ratio <= 1:
            raise ValueError(
                f"trigger_ratio must be in (0, 1], got {trigger_ratio!r}"
            )
        if reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must be >= 0")
        if tail_turns < 0:
            raise ValueError("tail_turns must be >= 0")
        if not 0 <= tail_budget_fraction <= 1:
            raise ValueError(
                f"tail_budget_fraction must be in [0, 1], got {tail_budget_fraction!r}"
            )
        self.trigger_ratio = trigger_ratio
        self.reserved_output_tokens = reserved_output_tokens
        self.tail_turns = tail_turns
        self.tail_budget_fraction = tail_budget_fraction
        self.prune_per_output_tokens = prune_per_output_tokens
        self.prune_total_threshold = prune_total_threshold
        self.summary_max_tokens = summary_max_tokens

    # ---- Public surface ---------------------------------------------------

    async def maybe_compact(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        model: "ResolvedModel",
        history: list[Message],
        new_messages: list[Message],
        tool_manager: "CompactionToolExecutor | None" = None,
        event_sink: "Callable[[StreamEvent], Awaitable[None]] | None" = None,
        max_tool_turns: int | None = None,
        principal: str | None = None,
        fixed_overhead: int = 0,
        last_compaction_tokens: int | None = None,
    ) -> CompactedTurn | None:
        """Decide whether to compact; if so, do it. Returns ``None`` if not.

        ``fixed_overhead`` is the estimated size of what goes out on every call and no
        history can give back (the rendered system prompt and the tool schemas; see
        :meth:`estimate_fixed_overhead`). The trigger compares ``history + new_messages +
        fixed_overhead`` and the result is measured again over the same population.

        ``last_compaction_tokens`` is the prompt size the newest compaction left (its marker's
        ``tokens_after``), when known. It is the strategy's only memory: when the trigger cannot be
        reached, a prompt that does not fit the budget is compacted again only once it has grown by
        a summary allowance beyond that size, so a compaction that bottoms out above the budget does
        not summarise its own summary on every turn.

        When ``tool_manager`` is supplied (the agent has
        ``compaction_tool_access`` on), the tier-2 summarisation call carries
        the agent's tools and runs a bounded, ephemeral tool-use loop; tool
        activity is streamed to ``event_sink`` but never enters the compacted
        history. When it is ``None`` the call is plain text-only summarisation.
        """
        new_tokens = self._estimate_tokens(new_messages)
        before = self._estimate_tokens(history) + new_tokens + fixed_overhead
        budget = self._effective_budget(model)
        trigger = int(self.trigger_ratio * budget)
        if before < trigger:
            return None

        # Tier 1: prune oversized tool outputs.
        pruned_history, pruned_count = self._prune_tool_outputs(
            history,
            per_output_threshold=self.prune_per_output_tokens,
            total_threshold=self.prune_total_threshold,
        )
        after_prune = self._estimate_tokens(pruned_history) + new_tokens + fixed_overhead
        if after_prune < trigger:
            # Pruning sufficed; rewrite history but skip the LLM summarisation.
            self._count("pruned")
            return CompactedTurn(
                new_messages=pruned_history,
                summary_message=None,
                pruned_tool_outputs=pruned_count,
                head_messages_replaced=0,
                estimated_tokens_before=before,
                estimated_tokens_after=after_prune,
                outcome="pruned",
                trigger_tokens=trigger,
                budget_tokens=budget,
                fixed_overhead_tokens=fixed_overhead,
            )

        # Tier 2: full compaction.
        return await self._tier2(
            pruned_history=pruned_history,
            pruned_count=pruned_count,
            before=before,
            agent=agent, llm=llm, model=model,
            tool_manager=tool_manager, event_sink=event_sink,
            max_tool_turns=max_tool_turns, principal=principal,
            fixed_overhead=fixed_overhead, extra_tokens=new_tokens, forced=False,
            last_compaction_tokens=last_compaction_tokens,
        )

    async def force_compact(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        model: "ResolvedModel",
        history: list[Message],
        tool_manager: "CompactionToolExecutor | None" = None,
        event_sink: "Callable[[StreamEvent], Awaitable[None]] | None" = None,
        max_tool_turns: int | None = None,
        principal: str | None = None,
        fixed_overhead: int = 0,
        new_messages: list[Message] | None = None,
    ) -> CompactedTurn:
        """Mandatory compaction (used by hard-overflow recovery).

        ``new_messages`` and ``fixed_overhead`` are only measured, as in :meth:`maybe_compact`
        (the new messages are not part of the history that is summarised)."""
        new_tokens = self._estimate_tokens(new_messages or [])
        before = self._estimate_tokens(history) + new_tokens + fixed_overhead
        pruned_history, pruned_count = self._prune_tool_outputs(
            history,
            per_output_threshold=self.prune_per_output_tokens,
            total_threshold=self.prune_total_threshold,
        )
        return await self._tier2(
            pruned_history=pruned_history,
            pruned_count=pruned_count,
            before=before,
            agent=agent, llm=llm, model=model,
            tool_manager=tool_manager, event_sink=event_sink,
            max_tool_turns=max_tool_turns, principal=principal,
            fixed_overhead=fixed_overhead, extra_tokens=new_tokens, forced=True,
        )

    async def _tier2(
        self,
        *,
        pruned_history: list[Message],
        pruned_count: int,
        before: int,
        agent: "Agent",
        llm: "LLM",
        model: "ResolvedModel",
        tool_manager: "CompactionToolExecutor | None" = None,
        event_sink: "Callable[[StreamEvent], Awaitable[None]] | None" = None,
        max_tool_turns: int | None = None,
        principal: str | None = None,
        fixed_overhead: int = 0,
        extra_tokens: int = 0,
        forced: bool = True,
        last_compaction_tokens: int | None = None,
    ) -> CompactedTurn:
        """Run the full LLM-driven compaction pass and assemble the
        :class:`CompactedTurn` result. Shared by :meth:`maybe_compact`
        (tier-2 fall-through) and :meth:`force_compact` (always-tier-2)
        to keep the result shape identical between the two paths.

        Every size here is over the population the trigger compares: the history that is
        summarised, plus ``extra_tokens`` (the new messages, which are never summarised) and
        ``fixed_overhead``. ``forced`` is a compaction that was asked for or that the provider
        demanded (an overflow, the manual route): it does not skip itself for being unable to
        reach the trigger, because the prompt is known not to fit. ``last_compaction_tokens`` is
        what the newest compaction left the prompt at (see :meth:`maybe_compact`); it only matters
        on the path that may skip."""
        budget = self._effective_budget(model)
        trigger = int(self.trigger_ratio * budget)
        history = pruned_history
        extra = fixed_overhead + extra_tokens
        history_tokens = self._estimate_tokens(history) + extra

        def unreducible(reason: str, outcome: str = "unreducible", detail: str = "") -> CompactedTurn:
            self._count(outcome)
            if outcome == "unreducible":
                self._warn(reason, tokens=history_tokens, trigger=trigger)
            else:
                logger.info(
                    "compaction skipped (%s): the prompt is about %d tokens against a trigger of %d; %s",
                    reason, history_tokens, trigger, detail,
                    extra={"reason": reason, "estimated_tokens": history_tokens, "trigger_tokens": trigger},
                )
            return CompactedTurn(
                new_messages=list(history),
                summary_message=None,
                pruned_tool_outputs=pruned_count,
                head_messages_replaced=0,
                estimated_tokens_before=before,
                estimated_tokens_after=history_tokens,
                unreducible=reason,
                outcome=outcome,
                trigger_tokens=trigger,
                budget_tokens=budget,
                fixed_overhead_tokens=fixed_overhead,
            )

        if fixed_overhead >= budget:
            # Named first: it is the cause whatever the history looks like, and no history change helps.
            return unreducible("fixed_over_budget")
        # The smallest tail the split may keep: the current turn's unanswered input (and
        # the newest unit). No summary can take the prompt below it.
        floor = self._split(history, tail_budget_tokens=0)
        if floor.reason is not None:
            # Nothing precedes the part that may not be summarised: summarising
            # "everything" would fold the question into the summary.
            return unreducible(floor.reason)
        floor_total = extra + self._estimate_tokens(floor.tail)
        if floor_total >= budget:
            # Not even a summary of everything else makes it fit: no model call, no marker that
            # copies the oversized input.
            return unreducible("protected_over_budget")
        if not forced and floor_total + self.summary_max_tokens >= trigger:
            # The best case (fixed + protected + a summary of the full allowance) is at or over the
            # trigger, so compacting cannot bring the prompt under it and would only run again every
            # turn. It is still worth doing for a prompt that does not fit the budget, once.
            #
            # Every size here is ``history_tokens``: the prompt AFTER the tier-1 prune, which is what is
            # sent (a skipped result carries the pruned history) and what a marker's ``tokens_after``
            # measures. ``before`` is the size before the prune, a prompt that is never sent: a turn that
            # reads a big file is far over the budget before the prune and under it after.
            if history_tokens < budget:
                return unreducible(
                    "cannot_reach_trigger", outcome="skipped",
                    detail="the prompt fits the window and even the smallest result would still be over the trigger",
                )
            if (
                last_compaction_tokens is not None
                and last_compaction_tokens <= model.context_length
                and history_tokens < last_compaction_tokens + self.summary_max_tokens
                and history_tokens < model.context_length
            ):
                # The newest compaction already left the prompt at about this size (a summary and the
                # protected input over the budget), and it has not grown by a summary allowance since:
                # compacting again would summarise the summary for no gain. Not for a prompt that does not
                # fit the window itself: that one would be rejected, so it is compacted whatever it grew by.
                # (Past the budget but inside the window the overflow path is still there as the net.)
                # And only a figure this window could have produced: one above it was left under a LARGER
                # window (the session's profile was switched since), says nothing about this prompt, and
                # would hold back a compaction the smaller window needs.
                return unreducible(
                    "recently_compacted", outcome="skipped",
                    detail=f"the last compaction left it at about {last_compaction_tokens} and it has not grown "
                           f"by {self.summary_max_tokens} since",
                )

        split = self._split(history, tail_budget_tokens=self._tail_budget(trigger - extra))
        summary_msg, reduction = await self._full_compact(
            head=split.summary_input, agent=agent, llm=llm, model=model, tool_manager=tool_manager,
            event_sink=event_sink, max_tool_turns=max_tool_turns, principal=principal,
        )
        compacted_messages = self._place(summary_msg, split)
        after = self._estimate_tokens(compacted_messages) + extra
        if after >= trigger and len(floor.tail) < len(split.tail):
            # Measure again: still over the trigger. One bounded escalation, in memory: summarise
            # again with only the protected part kept. TEXT-ONLY (no tool manager, no sink): a
            # tool-enabled summariser would run its tools a second time, and the marker is written
            # once, for the result that is returned.
            split = floor
            summary_msg, reduction = await self._full_compact(
                head=split.summary_input, agent=agent, llm=llm, model=model,
            )
            compacted_messages = self._place(summary_msg, split)
            after = self._estimate_tokens(compacted_messages) + extra
        insufficient = after >= trigger
        outcome = "insufficient" if insufficient else "summarised"
        self._count(outcome)
        if insufficient:
            self._warn("over_trigger", tokens=after, trigger=trigger)
        return CompactedTurn(
            new_messages=compacted_messages,
            summary_message=summary_msg,
            pruned_tool_outputs=pruned_count,
            head_messages_replaced=len(split.head),
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            unreducible="over_trigger" if insufficient else None,
            outcome=outcome,
            trigger_tokens=trigger,
            budget_tokens=budget,
            summary_after=split.summary_after,
            summary_input_reduced=reduction.as_payload() if reduction is not None else None,
            fixed_overhead_tokens=fixed_overhead,
        )

    @staticmethod
    def _place(summary: Message, split: "CompactionSplit") -> list[Message]:
        """The compacted history: the kept messages with the summary at its place (``summary_after``)."""
        k = split.summary_after
        return [*split.tail[:k], summary, *split.tail[k:]]

    def _split(self, history: list[Message], *, tail_budget_tokens: int):
        return split_for_compaction(
            history,
            tail_turns=self.tail_turns,
            tail_budget_tokens=tail_budget_tokens,
            size=self._estimate_tokens,
        )

    def reduced_target(self, model: "ResolvedModel") -> int:
        """The size, in estimated tokens, a prompt is reduced to when it has to shrink to be sent
        again: ``DEFAULT_REDUCED_FRACTION`` of the budget, so the reduced prompt leaves room for
        the reply and for the estimate being low."""
        return int(self.DEFAULT_REDUCED_FRACTION * self._effective_budget(model))

    def replay_guard(self, model: "ResolvedModel", *, fixed_overhead: int = 0) -> ReplayGuard:
        """The prompt guard for the replay after an overflow (see :class:`ReplayGuard`).

        What the guard measures is the whole call (the messages, the rendered system prompt among them, and the
        tool schemas), so its target is the fixed part plus ``DEFAULT_REDUCED_FRACTION`` of what the budget
        leaves after it: ``fixed + fraction * (budget - fixed)``. A fraction of the whole budget would count the
        fixed part against the history's share, and on an agent whose fixed part nearly fills the window it
        would leave the history nothing at all."""
        budget = self._effective_budget(model)
        target = fixed_overhead + int(self.DEFAULT_REDUCED_FRACTION * max(0, budget - fixed_overhead))
        return ReplayGuard(
            target_tokens=target,
            size=self._estimate_tokens,
            tools_size=lambda tools: self.estimate_fixed_overhead([], tools),
        )

    def newest_round_cap(self, model: "ResolvedModel", *, fixed_overhead: int, protected_tokens: int) -> int:
        """The most the newest folded round may weigh for a forced compaction to be able to keep it.

        A forced compaction protects the opening user input (``protected_tokens``) and the newest round,
        and its result is those two, the fixed part and a summary of the full allowance: what the budget
        leaves after the last three. ``0`` when nothing is left (the fixed part nearly fills the window):
        the round is then reduced to its placeholders, which is the most that can be done for it."""
        return max(
            0, self._effective_budget(model) - fixed_overhead - protected_tokens - self.summary_max_tokens,
        )

    def _tail_budget(self, room: int) -> int:
        """What the kept tail may weigh, given ``room`` (the trigger minus everything that is not
        history: the fixed overhead and the new messages): ``min(tail_budget_fraction * room,
        room - summary_max_tokens)``, so a summary of the full allowance plus the tail stays under
        the trigger. It is 0 whenever ``room`` is no larger than the summary allowance (a model
        with a context under about 4.6k tokens at the defaults, or a fixed part that takes most of
        the trigger): the tail then shrinks to its floor and the result is usually ``insufficient``,
        which is the honest verdict."""
        return max(0, min(int(self.tail_budget_fraction * room), room - self.summary_max_tokens))

    @staticmethod
    def estimate_fixed_overhead(system_messages: Sequence[Message], tools: Sequence["Tool"]) -> int:
        """The character-heuristic size of what goes out on every call and no history can give back:
        the rendered system prompt and the tool schemas. The same figure
        ``scripts/measure_fixed_overhead.py`` reports as ``heuristic``."""
        return count_tokens_char_fallback(messages=list(system_messages), tools=list(tools) or None)

    @staticmethod
    def _count(outcome: str) -> None:
        _metrics.compaction_outcomes_total.labels(outcome=outcome).inc()

    @staticmethod
    def _warn(reason: str, *, tokens: int, trigger: int) -> None:
        """A compaction that cannot reduce the prompt is a decision to send it anyway: say so."""
        if reason == "over_trigger":
            logger.warning(
                "compaction insufficient (%s): summarised, and the compacted prompt is still about %d tokens "
                "against a trigger of %d; what is left is the protected input and the fixed part",
                reason, tokens, trigger,
                extra={"reason": reason, "estimated_tokens": tokens, "trigger_tokens": trigger},
            )
            return
        logger.warning(
            "compaction unreducible (%s): the prompt is about %d tokens against a trigger of %d "
            "and nothing more can be summarised without dropping input the model has not answered",
            reason, tokens, trigger,
            extra={"reason": reason, "estimated_tokens": tokens, "trigger_tokens": trigger},
        )

    def _effective_budget(self, model: "ResolvedModel") -> int:
        """Token budget for live history before compaction triggers.

        Clamp the reserved-output allowance to at most half the model's
        context so the trigger cannot collapse to 0 for a small-context
        model. With the default 8192 reserved, an 8192-context model would
        otherwise get ``budget = 0`` -> ``trigger = 0`` -> compaction firing
        on EVERY turn (repeatedly summarising even a tiny history, which
        mangles short runs). Large-context models are unaffected
        (``min(8192, context//2) == 8192``).
        """
        reserved = min(
            self.reserved_output_tokens, max(1, model.context_length // 2)
        )
        return max(0, model.context_length - reserved)

    # ---- Token estimator --------------------------------------------------

    @staticmethod
    def _estimate_tokens(messages: Sequence[Message]) -> int:
        """Conservative character-heuristic token estimate.

        Per :class:`Part` type:

        * :class:`TextPart` -- ``ceil(len(text) / 4)``.
        * :class:`ToolCallPart` -- ``50 + len(name) + ceil(len(json.dumps(arguments)) / 4)``.
        * :class:`ToolResultPart` -- ``20 + ceil(len(output) / 4)``.
        * :class:`ImagePart` -- 1000 tokens (Anthropic / OpenAI ballpark).
        * :class:`DocumentPart` -- 2000 tokens (PDF page average).
        * :class:`ExtendedPart` (audio, video) -- 1500 tokens.
        * Plus 8 per message for role + envelope overhead.
        """
        total = 0
        for msg in messages:
            total += 8
            for part in msg.parts:
                total += CompactionStrategy._estimate_part_tokens(part)
        return total

    @staticmethod
    def _estimate_part_tokens(part: Part) -> int:
        if isinstance(part, TextPart):
            return -(-len(part.text) // 4)  # ceil division
        if isinstance(part, ToolCallPart):
            args_len = len(json.dumps(part.arguments, ensure_ascii=False))
            return 50 + len(part.name) + -(-args_len // 4)
        if isinstance(part, ToolResultPart):
            return 20 + -(-len(part.output) // 4)
        media = media_tokens(part)
        if media is not None:
            return media
        # Unknown / future part -- be conservative.
        return 200

    # ---- Pruning tier -----------------------------------------------------

    @staticmethod
    def _prune_tool_outputs(
        history: list[Message],
        *,
        per_output_threshold: int,
        total_threshold: int,
    ) -> tuple[list[Message], int]:
        """Replace oversized tool-result outputs with placeholder text."""
        result_token_estimates: list[tuple[int, int, int]] = []
        for mi, msg in enumerate(history):
            for pi, part in enumerate(msg.parts):
                if isinstance(part, ToolResultPart):
                    tokens = 20 + -(-len(part.output) // 4)
                    result_token_estimates.append((mi, pi, tokens))

        total_tokens = sum(t for _, _, t in result_token_estimates)
        if total_tokens <= total_threshold:
            return list(history), 0

        to_prune = {
            (mi, pi)
            for (mi, pi, t) in result_token_estimates
            if t > per_output_threshold
        }
        if not to_prune:
            return list(history), 0

        new_history: list[Message] = []
        pruned_count = 0
        for mi, msg in enumerate(history):
            replaced = False
            new_parts: list[Part] = []
            for pi, part in enumerate(msg.parts):
                if (mi, pi) in to_prune and isinstance(part, ToolResultPart):
                    placeholder = (
                        f"[output of {len(part.output)} chars omitted by "
                        "compaction; check the persisted history if you need "
                        "the full text]"
                    )
                    new_parts.append(
                        ToolResultPart(
                            id=part.id,
                            output=placeholder,
                            error=part.error,
                        )
                    )
                    replaced = True
                    pruned_count += 1
                else:
                    new_parts.append(part)
            if replaced:
                new_history.append(Message(role=msg.role, parts=new_parts))
            else:
                new_history.append(msg)

        return new_history, pruned_count

    # ---- Full-compaction tier --------------------------------------------

    async def _full_compact(
        self,
        *,
        head: list[Message],
        agent: "Agent",
        llm: "LLM",
        model: "ResolvedModel",
        tool_manager: "CompactionToolExecutor | None" = None,
        event_sink: "Callable[[StreamEvent], Awaitable[None]] | None" = None,
        max_tool_turns: int | None = None,
        principal: str | None = None,
    ) -> tuple[Message, SummaryInputReduction | None]:
        """Summarise ``head`` into one assistant-role message. The split (what is head,
        what is kept) is decided by the caller: see :func:`split_for_compaction`.

        The summariser is a model call too, and a head over its window makes it overflow. That is
        recovered ONCE, here, so every path that summarises (the proactive compaction, the forced one,
        the manual route) gets it: the call is made again TEXT ONLY (no tools, so no tool runs twice and
        the tool schemas stop counting) on an input reduced to what fits (:func:`reduce_summary_input`:
        tool results left out, then a bounded rolling fold, then single units cut). What was done comes
        back beside the summary, for the marker. An overflow that cannot be reduced away, or that repeats,
        is :class:`SummariserOverflow`, which names the summariser."""
        compaction_prompt = (
            "\n\n".join(agent.compaction_prompt)
            if agent.compaction_prompt
            else DEFAULT_COMPACTION_PROMPT
        )
        summary_request = self._summary_request(compaction_prompt, head)
        reduction: SummaryInputReduction | None = None
        carried: list[int] = []  # what a tool loop's earlier rounds added to the call that overflowed
        try:
            if tool_manager is None:
                summary_text = await self._summarise_text_only(summary_request, llm=llm, model=model)
            else:
                summary_text, cut_round = await self._summarise_with_tools(
                    summary_request,
                    llm=llm,
                    model=model,
                    tool_manager=tool_manager,
                    event_sink=event_sink,
                    max_tool_turns=max_tool_turns,
                    principal=principal,
                    carried=carried,
                )
                if cut_round:
                    # The loop overflowed in a later round and ended with the summary it had written: that is an
                    # overflow too, and the marker must say the summary may be less than one of the whole head.
                    reduction = SummaryInputReduction(tool_loop_cut_round=cut_round)
        except Exception as exc:  # noqa: BLE001 -- only a context overflow is this recovery's
            if not is_context_overflow(exc):
                raise
            logger.warning(
                "compaction: the summariser's own call overflowed; reducing its input and retrying once, text only",
                extra={"error": str(exc), "tools": tool_manager is not None},
            )
            summary_text, reduction = await self._summarise_reduced(
                head, compaction_prompt=compaction_prompt, llm=llm, model=model, cause=exc,
                first_call_extra=await self._tool_schema_tokens(tool_manager, principal) + sum(carried),
                head_known_to_fit=sum(carried) > 0,   # an earlier round was accepted with the head in it
            )

        marker_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary_msg = CompactionSummary(
            role="assistant",
            parts=[
                TextPart(
                    text=(
                        f"[earlier conversation compacted on {marker_ts}]\n\n"
                        f"{summary_text}"
                    )
                )
            ],
        )
        return summary_msg, reduction

    async def _tool_schema_tokens(
        self, tool_manager: "CompactionToolExecutor | None", principal: str | None,
    ) -> int:
        """What the tool catalogue adds to a tool-enabled summariser's call (``0`` for a text-only one)."""
        if tool_manager is None:
            return 0
        try:
            return self.estimate_fixed_overhead([], await tool_manager.list_tools(principal=principal))
        except Exception:  # noqa: BLE001 -- an estimate for the retry's sizing: not worth failing the recovery for
            logger.debug("compaction: the tool catalogue could not be sized for the summariser's retry", exc_info=True)
            return 0

    @staticmethod
    def _summary_request(compaction_prompt: str, chunk: Sequence[Message], earlier: str | None = None) -> list[Message]:
        """What one summariser call is sent: the compaction prompt, then (in a rolling fold) the summary so far,
        then the part of the head it reads, then the instruction to produce the summary."""
        return [
            Message(role="system", parts=[TextPart(text=compaction_prompt)]),
            *([Message(role="assistant", parts=[TextPart(text=f"[the summary so far]\n\n{earlier}")])] if earlier else []),
            *chunk,
            Message(
                role="user",
                parts=[
                    TextPart(
                        text=(
                            "Now produce the summary as instructed. "
                            "One dense paragraph; no headers, no lists."
                        )
                    )
                ],
            ),
        ]

    async def _summarise_reduced(
        self,
        head: list[Message],
        *,
        compaction_prompt: str,
        llm: "LLM",
        model: "ResolvedModel",
        cause: BaseException,
        first_call_extra: int = 0,
        head_known_to_fit: bool = False,
    ) -> tuple[str, SummaryInputReduction]:
        """The one retry of a summariser call that overflowed: text only, on a reduced input."""
        current = self._estimate_tokens(head)
        try:
            sizing = size_summariser_input(
                window=model.context_length,
                budget=self._effective_budget(model),
                summary_tokens=self.summary_max_tokens,
                frame=self._estimate_tokens(self._summary_request(compaction_prompt, [])),
                current=current,
                first_call_extra=first_call_extra,
                head_known_to_fit=head_known_to_fit,
            )
        except SummaryInputUnreachable as unreachable:
            raise SummariserOverflow(
                f"the compaction's summariser was rejected as too large and cannot be retried: {unreachable}",
                cause=cause,
            ) from cause
        goal = sizing.goal
        try:
            reduced = reduce_summary_input(
                head,
                size=self._estimate_tokens,
                part_size=self._estimate_part_tokens,
                goal_tokens=goal,
                chunk_tokens=sizing.chunk_tokens,
                max_chunks=sizing.max_chunks,
            )
        except SummaryInputUnreachable as unreachable:
            raise SummariserOverflow(
                f"the compaction's summariser was rejected as too large and its input could not be reduced "
                f"to fit: {unreachable} (about {current} tokens to summarise, {goal} fit)",
                cause=cause,
            ) from cause
        try:
            earlier: str | None = None
            for chunk in reduced.chunks:
                earlier = await self._summarise_text_only(
                    self._summary_request(compaction_prompt, chunk, earlier), llm=llm, model=model,
                )
        except Exception as exc:  # noqa: BLE001
            if not is_context_overflow(exc):
                raise
            raise SummariserOverflow(
                f"the compaction's summariser was rejected as too large again after its input was reduced "
                f"(pruned {reduced.report.pruned}, {len(reduced.chunks)} call(s), "
                f"truncated {reduced.report.truncated_parts}); not retrying a second time",
                cause=exc,
            ) from exc
        assert earlier is not None
        return earlier, reduced.report

    async def _summarise_text_only(
        self,
        summary_request: list[Message],
        *,
        llm: "LLM",
        model: "ResolvedModel",
    ) -> str:
        """Plain, tool-free summarisation (unchanged legacy path)."""
        text_buffers: list[str] = []
        async for event in llm.stream(
            model=model.model_name,
            messages=summary_request,
            temperature=0.0,
            max_output_tokens=self.summary_max_tokens,
        ):
            if isinstance(event, TextDelta):
                text_buffers.append(event.text)
            elif isinstance(event, Error) and event.fatal:
                raise ServerError(
                    f"compaction LLM failed: {event.message}",
                    code=event.code,
                )
            # Done / Usage / other events ignored; we only need the text.

        summary_text = "".join(text_buffers).strip()
        if not summary_text:
            raise ServerError("compaction produced empty summary text")
        return summary_text

    async def _summarise_with_tools(
        self,
        summary_request: list[Message],
        *,
        llm: "LLM",
        model: "ResolvedModel",
        tool_manager: "CompactionToolExecutor",
        event_sink: "Callable[[StreamEvent], Awaitable[None]] | None",
        max_tool_turns: int | None,
        principal: str | None,
        carried: list[int] | None = None,
    ) -> tuple[str, int]:
        """Tool-enabled summarisation: a bounded, ephemeral tool-use loop.

        The compaction prompt may instruct the model to call the agent's tools
        (e.g. dump the compacted content to workspace files). Tool calls are
        executed via ``tool_manager`` and their lifecycle events forwarded to
        ``event_sink`` (surfaced as debug/activity events); the intermediate
        assistant/tool messages are DISCARDED -- only the model's final text is
        returned as the summary. An empty final text (e.g. the model spent its
        turn writing files) falls back to a marker rather than erroring.

        Returns ``(summary_text, cut_round)``: ``cut_round`` is the round (1-based) in which the loop overflowed
        and was ended with the summary it had already written, else ``0``.
        """
        cap = (
            max_tool_turns
            if max_tool_turns is not None and max_tool_turns > 0
            else DEFAULT_COMPACTION_TOOL_TURNS
        )
        tools = await tool_manager.list_tools(principal=principal)
        messages = list(summary_request)
        summary_text = ""
        tool_round = 0
        cut_round = 0
        while True:
            buffered: list[StreamEvent] = []
            try:
                async for event in llm.stream(
                    model=model.model_name,
                    messages=messages,
                    temperature=0.0,
                    max_output_tokens=self.summary_max_tokens,
                    tools=tools,
                    tool_choice="auto",
                ):
                    buffered.append(event)
                    if isinstance(event, (ToolCallStart, ToolCallEnd)):
                        await self._sink(event_sink, event)
                    elif isinstance(event, Error) and event.fatal:
                        raise ServerError(
                            f"compaction LLM failed: {event.message}",
                            code=event.code,
                        )
            except Exception as exc:  # noqa: BLE001 -- only a context overflow is handled here
                # An overflow after a round that wrote a summary is the tool results outgrowing the window:
                # the tools have run (their effects stand), so the loop ends with that summary instead of
                # starting again. With none yet (round one, or rounds that only called tools) the overflow
                # goes up and ``_full_compact`` makes one text-only call on a reduced input, which runs no tool.
                if not summary_text or not is_context_overflow(exc):
                    if carried is not None and is_context_overflow(exc):
                        # the rounds before this one are in the call that overflowed and not in the text-only
                        # retry: the sizing must not read the head's own size as the whole explanation
                        carried.append(self._estimate_tokens(messages[len(summary_request):]))
                    raise
                logger.warning(
                    "compaction: the summariser's tool loop overflowed in round %d; ending it with the "
                    "summary written so far", tool_round + 1, extra={"error": str(exc)},
                )
                cut_round = tool_round + 1
                break
            try:
                assistant_msg = output_to_message(buffered)
            except ValueError:
                # Empty / error-only stream -- nothing more to do.
                break

            tool_calls = [
                p for p in assistant_msg.parts if isinstance(p, ToolCallPart)
            ]
            # Keep the last NON-EMPTY assistant text as the summary. A prompt
            # that emits the summary first and only then dumps to files ends on
            # tool-only (empty-text) rounds; without this guard that trailing
            # empty round would clobber the real summary with the fallback.
            round_text = "".join(
                p.text for p in assistant_msg.parts if isinstance(p, TextPart)
            ).strip()
            if round_text:
                summary_text = round_text
            if not tool_calls:
                break

            tool_round += 1
            if tool_round >= cap:
                logger.warning(
                    "compaction: reached tool-turn cap (%d); force-stopping "
                    "the compaction tool loop", cap,
                )
                break

            result_parts: list[ToolResultPart] = []
            for call in tool_calls:
                try:
                    rp = await tool_manager.execute(call, principal=principal)
                except Exception as exc:  # noqa: BLE001
                    # INTENTIONAL divergence from the turn loop's contract
                    # (loop.py / base.py re-raise AuthRequiredError + let
                    # YieldToWorker propagate): compaction runs OUTSIDE the
                    # turn's park/auth machinery, so propagating either would
                    # corrupt park/resume state. Swallowing is safe -- the
                    # approval gate raises YieldToWorker *before* dispatch, so
                    # this fails closed (no un-approved side effect). Net effect:
                    # auth/approval/yielding tools (ask_user, sleep, watch_files)
                    # simply produce an error result during compaction and the
                    # model moves on. Do NOT "harmonize" this to re-raise.
                    # (CancelledError is a BaseException and still propagates.)
                    rp = ToolResultPart(id=call.id, output=redact_credentials(str(exc)), error=True)
                result_parts.append(rp)
                await self._sink(
                    event_sink,
                    ExtendedEvent(
                        extended=_ExecutorToolResult(
                            call_id=rp.id, output=rp.output, error=rp.error,
                        )
                    ),
                )
            messages = messages + [
                assistant_msg,
                Message(role="tool", parts=result_parts),
            ]

        if not summary_text:
            summary_text = (
                "(compaction delegated the detail to tools -- the earlier "
                "conversation was written out via the compaction tool calls; "
                "see the compaction activity for what was produced.)"
            )
        return summary_text, cut_round

    @staticmethod
    async def _sink(
        event_sink: "Callable[[StreamEvent], Awaitable[None]] | None",
        event: StreamEvent,
    ) -> None:
        """Forward a compaction tool event to the sink, swallowing sink errors
        (observability must never break compaction)."""
        if event_sink is None:
            return
        try:
            await event_sink(event)
        except Exception:  # noqa: BLE001
            logger.debug("compaction: event_sink raised; ignoring", exc_info=True)


__all__ = [
    "CompactedTurn",
    "CompactionStrategy",
    "CompactionToolExecutor",
    "DEFAULT_COMPACTION_TOOL_TURNS",
    "DEFAULT_CONTEXT_LIMIT",
    "MODEL_CONTEXT_FALLBACK",
    "lookup_context_length",
]
