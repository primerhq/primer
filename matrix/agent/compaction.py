"""Two-tier history compaction for the agent executors.

The :class:`CompactionStrategy` is shared between
:class:`matrix.agent.AgentExecutor` (chat threads) and
:class:`matrix.agent.WorkspaceAgentExecutor` (workspace-backed). It is
called between turns to keep the prompt under the configured LLM's
context limit.

Two tiers:

1. **Pruning (cheap)** -- replace oversized tool-result outputs with
   placeholder text in-place. The call/result envelope is preserved
   so the LLM doesn't see orphaned tool calls.
2. **Full compaction (expensive)** -- replace the head of the history
   with one assistant-role summary message produced by calling the
   same LLM with the agent's :attr:`Agent.compaction_prompt` (or the
   system default).

Token counting uses a conservative character heuristic refined by the
running ``Usage.input_tokens`` from the most recent turn.

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
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from matrix.agent.prompts import DEFAULT_COMPACTION_PROMPT
from matrix.agent.tail import tail_split
from matrix.model.chat import (
    AudioPart,
    DocumentPart,
    Error,
    ExtendedPart,
    ImagePart,
    Message,
    Part,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    VideoPart,
)
from matrix.model.except_ import ServerError


if TYPE_CHECKING:
    from matrix.int.llm import LLM
    from matrix.model.agent import Agent
    from matrix.model.provider import LLMModel


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-model context-length fallback table.
# ---------------------------------------------------------------------------
#
# Used when the resolved :class:`LLMModel.context_length` is unavailable
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

    1. ``configured`` -- the :attr:`LLMModel.context_length` from the
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
    :data:`matrix.agent.prompts.DEFAULT_COMPACTION_PROMPT`. Compaction
    summarisation calls back into the SAME ``llm`` / ``model`` the
    agent uses (cheaper than running a separate model per the
    user-confirmed design decision).
    """

    DEFAULT_TRIGGER_RATIO: float = 0.85
    DEFAULT_RESERVED_OUTPUT: int = 8192
    DEFAULT_TAIL_TURNS: int = 4
    DEFAULT_PRUNE_PER_OUTPUT: int = 20_000
    DEFAULT_PRUNE_TOTAL_THRESHOLD: int = 40_000
    DEFAULT_SUMMARY_MAX_TOKENS: int = 2048

    def __init__(
        self,
        *,
        trigger_ratio: float = DEFAULT_TRIGGER_RATIO,
        reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT,
        tail_turns: int = DEFAULT_TAIL_TURNS,
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
        self.trigger_ratio = trigger_ratio
        self.reserved_output_tokens = reserved_output_tokens
        self.tail_turns = tail_turns
        self.prune_per_output_tokens = prune_per_output_tokens
        self.prune_total_threshold = prune_total_threshold
        self.summary_max_tokens = summary_max_tokens

    # ---- Public surface ---------------------------------------------------

    async def maybe_compact(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        model: "LLMModel",
        history: list[Message],
        new_messages: list[Message],
        last_known_input_tokens: int | None = None,
    ) -> CompactedTurn | None:
        """Decide whether to compact; if so, do it. Returns ``None`` if not."""
        before = max(
            self._estimate_tokens([*history, *new_messages]),
            last_known_input_tokens or 0,
        )
        budget = max(0, model.context_length - self.reserved_output_tokens)
        trigger = int(self.trigger_ratio * budget)
        if before < trigger:
            return None

        # Tier 1: prune oversized tool outputs.
        pruned_history, pruned_count = self._prune_tool_outputs(
            history,
            per_output_threshold=self.prune_per_output_tokens,
            total_threshold=self.prune_total_threshold,
        )
        after_prune = max(
            self._estimate_tokens([*pruned_history, *new_messages]),
            last_known_input_tokens or 0,
        )
        if after_prune < trigger:
            # Pruning sufficed; rewrite history but skip the LLM summarisation.
            return CompactedTurn(
                new_messages=pruned_history,
                summary_message=None,
                pruned_tool_outputs=pruned_count,
                head_messages_replaced=0,
                estimated_tokens_before=before,
                estimated_tokens_after=after_prune,
            )

        # Tier 2: full compaction.
        return await self._tier2(
            pruned_history=pruned_history,
            pruned_count=pruned_count,
            before=before,
            agent=agent, llm=llm, model=model,
        )

    async def force_compact(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        model: "LLMModel",
        history: list[Message],
    ) -> CompactedTurn:
        """Mandatory compaction (used by hard-overflow recovery)."""
        before = self._estimate_tokens(history)
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
        )

    async def _tier2(
        self,
        *,
        pruned_history: list[Message],
        pruned_count: int,
        before: int,
        agent: "Agent",
        llm: "LLM",
        model: "LLMModel",
    ) -> CompactedTurn:
        """Run the full LLM-driven compaction pass and assemble the
        :class:`CompactedTurn` result. Shared by :meth:`maybe_compact`
        (tier-2 fall-through) and :meth:`force_compact` (always-tier-2)
        to keep the result shape identical between the two paths."""
        compacted_messages, summary_msg, head_count = await self._full_compact(
            history=pruned_history,
            agent=agent,
            llm=llm,
            model=model,
        )
        after = self._estimate_tokens(compacted_messages)
        return CompactedTurn(
            new_messages=compacted_messages,
            summary_message=summary_msg,
            pruned_tool_outputs=pruned_count,
            head_messages_replaced=head_count,
            estimated_tokens_before=before,
            estimated_tokens_after=after,
        )

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
        if isinstance(part, ImagePart):
            return 1_000
        if isinstance(part, DocumentPart):
            return 2_000
        if isinstance(part, ExtendedPart):
            inner = part.extended
            if isinstance(inner, (AudioPart, VideoPart)):
                return 1_500
            return 500
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
        history: list[Message],
        agent: "Agent",
        llm: "LLM",
        model: "LLMModel",
    ) -> tuple[list[Message], Message | None, int]:
        head, tail = tail_split(history, tail_turns=self.tail_turns)
        if not head:
            # Nothing to summarise; pruning alone is the only lever.
            return list(tail), None, 0

        compaction_prompt = (
            "\n\n".join(agent.compaction_prompt)
            if agent.compaction_prompt
            else DEFAULT_COMPACTION_PROMPT
        )

        summary_request: list[Message] = [
            Message(role="system", parts=[TextPart(text=compaction_prompt)]),
            *head,
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

        text_buffers: list[str] = []
        async for event in llm.stream(
            model=model.name,
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

        marker_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary_msg = Message(
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
        return [summary_msg, *tail], summary_msg, len(head)


__all__ = [
    "CompactedTurn",
    "CompactionStrategy",
    "DEFAULT_CONTEXT_LIMIT",
    "MODEL_CONTEXT_FALLBACK",
    "lookup_context_length",
]
