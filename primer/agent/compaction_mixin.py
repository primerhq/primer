"""Shared compaction primitives consumed by both runners.

Free async functions used by :class:`_BaseAgentExecutor` (workspace
+ thread executor) and :class:`primer.chat.executor.ChatTurnRunner`.
Both runners gain identical auto-compact behaviour by delegating to
this module.

The mixin is stateless. Callers own the persistence side -- this
module only computes whether compaction should fire, runs the
:class:`CompactionStrategy`, and returns a structured result.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from primer.model.chat import Message, Tool


if TYPE_CHECKING:
    from primer.agent.compaction import CompactionStrategy
    from primer.int.llm import LLM


logger = logging.getLogger(__name__)


DEFAULT_TRIGGER_RATIO: float = 0.90
DEFAULT_RESERVED_OUTPUT_TOKENS: int = 2000


@dataclass
class CompactionResult:
    """Outcome of an ``apply_compaction`` / ``force_compact`` call.

    The ``replaced_from_seq`` / ``replaced_to_seq`` fields are populated
    by the caller (which has access to the chat-side seqs). The mixin
    itself sets them to ``None`` on the workspace/thread path.
    """

    new_history: list[Message]
    summary_text: str
    tokens_before: int
    tokens_after: int
    model: str
    created_at: datetime
    replaced_from_seq: int | None = None
    replaced_to_seq: int | None = None
    pruned_tool_outputs: int = 0
    head_messages_replaced: int = 0


async def should_compact(
    *,
    llm: "LLM",
    model_name: str,
    context_length: int,
    history: Sequence[Message],
    tools: Sequence[Tool] | None = None,
    trigger_ratio: float = DEFAULT_TRIGGER_RATIO,
    reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
) -> tuple[bool, int]:
    """Decide whether the next turn should be preceded by compaction.

    Threshold: ``count >= (context_length - reserved_output_tokens) * trigger_ratio``.
    """
    count = await llm.count_tokens(
        model=model_name,
        messages=list(history),
        tools=list(tools) if tools else None,
    )
    budget = max(0, context_length - reserved_output_tokens)
    trigger = int(trigger_ratio * budget)
    return count >= trigger, count


def _clone_strategy_for_apply(
    strategy: "CompactionStrategy",
    history: Sequence[Message],
) -> "CompactionStrategy":
    """Return a strategy tuned so ``_full_compact`` actually summarises.

    When the caller's strategy keeps a generous tail (e.g.
    ``tail_turns=4`` default) and the history is short, ``tail_split``
    would put every message in the tail and the LLM summarisation pass
    would be skipped. ``apply_compaction`` is an explicit request to
    compact, so we clamp ``tail_turns`` to leave at least one head
    message whenever any assistant messages exist.
    """
    from primer.agent.compaction import CompactionStrategy

    assistant_count = sum(1 for m in history if m.role == "assistant")
    max_tail = max(0, assistant_count - 1)
    effective_tail = min(strategy.tail_turns, max_tail)
    if effective_tail == strategy.tail_turns:
        return strategy
    return CompactionStrategy(
        trigger_ratio=strategy.trigger_ratio,
        reserved_output_tokens=strategy.reserved_output_tokens,
        tail_turns=effective_tail,
        prune_per_output_tokens=strategy.prune_per_output_tokens,
        prune_total_threshold=strategy.prune_total_threshold,
        summary_max_tokens=strategy.summary_max_tokens,
    )


async def apply_compaction(
    *,
    llm: "LLM",
    strategy: "CompactionStrategy",
    history: list[Message],
    compaction_prompt: str,
    model_name: str,
    context_length: int,
) -> CompactionResult:
    """Run the :class:`CompactionStrategy` and assemble a :class:`CompactionResult`.

    The strategy expects an Agent-like shim with a ``compaction_prompt``
    field (``list[str]``), and a model shim with ``name`` +
    ``context_length``. Both shapes match the duck-typed access pattern
    used by :meth:`CompactionStrategy._full_compact` and
    :meth:`CompactionStrategy._tier2`.
    """

    class _AgentShim:
        def __init__(self) -> None:
            self.compaction_prompt: list[str] = (
                [compaction_prompt] if compaction_prompt else []
            )

    class _ModelShim:
        def __init__(self) -> None:
            self.name = model_name
            self.context_length = context_length

    effective_strategy = _clone_strategy_for_apply(strategy, history)
    compacted = await effective_strategy._tier2(  # noqa: SLF001
        pruned_history=list(history),
        pruned_count=0,
        before=0,
        agent=_AgentShim(),
        llm=llm,
        model=_ModelShim(),
    )
    summary_msg = compacted.summary_message
    summary_text = (
        summary_msg.parts[0].text
        if summary_msg and summary_msg.parts
        else ""
    )
    return CompactionResult(
        new_history=list(compacted.new_messages),
        summary_text=summary_text,
        tokens_before=compacted.estimated_tokens_before,
        tokens_after=compacted.estimated_tokens_after,
        model=model_name,
        created_at=datetime.now(timezone.utc),
        replaced_from_seq=None,
        replaced_to_seq=None,
        pruned_tool_outputs=compacted.pruned_tool_outputs,
        head_messages_replaced=compacted.head_messages_replaced,
    )


async def force_compact(
    *,
    llm: "LLM",
    strategy: "CompactionStrategy",
    history: list[Message],
    compaction_prompt: str,
    model_name: str,
    context_length: int,
) -> CompactionResult:
    """On-demand compaction -- bypasses the trigger check."""
    return await apply_compaction(
        llm=llm,
        strategy=strategy,
        history=history,
        compaction_prompt=compaction_prompt,
        model_name=model_name,
        context_length=context_length,
    )


__all__ = [
    "CompactionResult",
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "DEFAULT_TRIGGER_RATIO",
    "apply_compaction",
    "force_compact",
    "should_compact",
]
