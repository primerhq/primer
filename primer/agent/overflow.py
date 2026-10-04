"""Context-overflow recovery helpers shared by the executor and the compaction strategy.

Three small pieces, none of which does I/O:

* :func:`is_context_overflow` decides whether a ``BadRequestError`` is an INPUT overflow (the only
  kind that compaction can fix);
* :class:`ReplayGuard` is the :class:`primer.agent.loop.PromptGuard` the executor installs for the
  replay that follows a forced compaction: it reduces the tool results in the OUTGOING prompt
  (the S3 pruning primitive, forced) so a carried result that was itself the reason the call
  overflowed cannot overflow the replay too;
* :func:`reduce_head_for_summary` gives the summariser a smaller copy of the history it is asked to
  summarise after its first attempt was rejected as too large.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from primer.agent.prune import PruneSet, prune_prompt
from primer.model.chat import Message, TextPart, Tool
from primer.model.except_ import BadRequestError

SizeOf = Callable[[Sequence[Message]], int]


def is_context_overflow(exc: BadRequestError) -> bool:
    """Is this ``BadRequestError`` a rejection of the INPUT's size?

    The shipped LLM adapters wrap provider exceptions into ``BadRequestError`` without a stable
    code for context overflow, so this matches the providers' wording. It must not match an
    OUTPUT-limit rejection (``max_tokens is too large``, ``maximum allowed number of output
    tokens``) or an unrelated "too long" (a tool name, a file name): recovering from those by
    compacting writes a lossy durable summary for an error no summary can fix.
    """
    msg = (exc.message or "").lower()
    needles = (
        "context length",
        "context_length",
        "context window",
        "maximum context",
        "context limit",
        "input is too long",
        "prompt is too long",
        "tokens exceeds",
        "token limit",
        # Gemini: "The input token count (N) exceeds the maximum number of tokens allowed (M)."
        "exceeds the maximum number of tokens",
    )
    return any(n in msg for n in needles)


def whole_rounds(messages: Sequence[Message]) -> list[Message]:
    """``messages`` without a trailing assistant tool call that no tool message answered.

    A turn's in-flight messages are whole ``(assistant tool_use, tool results)`` rounds when an
    overflow is raised by an LLM call; this drops the one exception (a failure between a call's
    reply and its results) so a replay never sends a tool call without its result, which
    providers reject.
    """
    out = list(messages)
    if out and out[-1].role == "assistant" and any(p.type == "tool_call" for p in out[-1].parts):
        out.pop()
    return out


def tool_rounds(messages: Sequence[Message]) -> int:
    """How many tool rounds ``messages`` hold: assistant messages that carry a tool call."""
    return sum(1 for m in messages if m.role == "assistant" and any(p.type == "tool_call" for p in m.parts))


class ReplayGuard:
    """Reduce the tool results of the outgoing prompt to a size target, before every call.

    Installed only for the replay after an overflow. Each call it re-applies what it pruned on
    earlier calls (the prompt of one turn is append-only, so a recorded prune set stays valid),
    measures the result, and when that is still over ``target_tokens`` prunes the excess with the
    forced mode: older results are replaced by a placeholder, then the largest remaining ones,
    the newest rounds included, are truncated. It never touches the caller's own messages, so
    the persisted record keeps the raw results. It reduces what it can: a prompt that is over the
    target with nothing left to shed is sent as it is.
    """

    def __init__(self, *, target_tokens: int, size: SizeOf) -> None:
        self._target = target_tokens
        self._size = size
        self._sticky = PruneSet()
        self.shed_calls = 0

    async def before_call(self, prompt: list[Message], *, tools: list[Tool]) -> list[Message]:
        outcome = prune_prompt(prompt, sticky=self._sticky)
        over = self._size(outcome.messages) - self._target
        if over > 0:
            outcome = prune_prompt(prompt, sticky=self._sticky, shed_tokens=over, force=True)
            self.shed_calls += 1
        self._sticky = outcome.prune_set
        return outcome.messages

    def after_call(self, usage) -> None:
        return None


_CUT = "\n[... {n} characters omitted before summarising ...]\n"


def reduce_head_for_summary(
    head: Sequence[Message], *, target_tokens: int, size: SizeOf,
) -> list[Message]:
    """A smaller copy of ``head`` for the summariser: results pruned, long texts cut.

    First the tool results are reduced with the forced prune (no result is protected: the head is
    old by definition), then, if the copy is still over ``target_tokens``, every text part longer
    than an even share of the target is cut to that share (two thirds from its start, one third
    from its end, the cut marked). Tool calls and the message order are untouched. The input is
    not modified; the persisted history is only replaced by the marker the summary produces.
    """
    reduced = list(head)
    over = size(reduced) - target_tokens
    if over > 0:
        reduced = prune_prompt(reduced, shed_tokens=over, force=True, keep_rounds=0).messages
    if size(reduced) <= target_tokens:
        return reduced
    texts = sum(1 for m in reduced for p in m.parts if isinstance(p, TextPart))
    cap = max(2_000, target_tokens * 4 // max(1, texts))
    out: list[Message] = []
    for message in reduced:
        parts = []
        for part in message.parts:
            if isinstance(part, TextPart) and len(part.text) > cap:
                keep_head = cap * 2 // 3
                keep_tail = cap - keep_head
                cut = len(part.text) - keep_head - keep_tail
                part = part.model_copy(update={
                    "text": part.text[:keep_head] + _CUT.format(n=cut) + part.text[len(part.text) - keep_tail:],
                })
            parts.append(part)
        out.append(message.model_copy(update={"parts": parts}))
    return out


__all__ = [
    "ReplayGuard",
    "is_context_overflow",
    "reduce_head_for_summary",
    "tool_rounds",
    "whole_rounds",
]
