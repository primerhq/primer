"""Tail-split helpers for the compaction strategy.

Splits a chat history into ``(head, tail)`` so the compactor can
replace the head with a summary while keeping the most-recent N
assistant turns verbatim.

:func:`tail_split` is the plain turn-based split. :func:`split_for_compaction`
is what tier 2 uses: the same turn-based tail, made safe to summarise around
(never splits a tool call from its results, never summarises input the model
has not answered yet, and bounds the tail by size so it cannot keep the prompt
over the trigger).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from primer.model.chat import Message, ToolCallPart


def tail_split(
    messages: Sequence[Message],
    *,
    tail_turns: int,
) -> tuple[list[Message], list[Message]]:
    """Split ``messages`` into ``(head, tail)`` at the Nth-most-recent assistant boundary.

    A *turn boundary* is the index of any :class:`Message` with
    ``role == "assistant"``. The tail starts at the
    ``tail_turns``-th-most-recent assistant message (counting from 1
    at the end of ``messages``); everything before is the head.

    * ``tail_turns == 0`` -- tail is empty, head is the full input.
    * ``tail_turns >= count(assistant_messages)`` -- head is empty,
      tail is the full input.
    * ``tail_turns < 0`` -- raises :class:`ValueError`.

    Returns the split as two lists. Caller is responsible for
    inserting the summary in front of the tail when reassembling.
    """
    if tail_turns < 0:
        raise ValueError(f"tail_turns must be >= 0, got {tail_turns!r}")
    if tail_turns == 0:
        return list(messages), []

    assistant_indices = [
        i for i, m in enumerate(messages) if m.role == "assistant"
    ]
    if not assistant_indices:
        # No assistant boundaries -> nothing to summarise; head IS everything.
        return list(messages), []
    if len(assistant_indices) < tail_turns:
        # Asked for more tail-turns than exist -> keep everything verbatim in tail.
        return [], list(messages)

    boundary = assistant_indices[-tail_turns]
    return list(messages[:boundary]), list(messages[boundary:])


def _has_tool_call(message: Message) -> bool:
    return message.role == "assistant" and any(isinstance(p, ToolCallPart) for p in message.parts)


def unit_starts(messages: Sequence[Message]) -> list[int]:
    """The index where each ATOMIC unit of ``messages`` begins.

    An assistant message that carries tool calls and the tool messages that answer
    it are ONE unit: a boundary between them leaves a tool call without its result
    or a result without its call, which providers reject. Every other message is a
    unit of its own.
    """
    starts: list[int] = []
    i, n = 0, len(messages)
    while i < n:
        starts.append(i)
        call = _has_tool_call(messages[i])
        i += 1
        if call:
            while i < n and messages[i].role == "tool":
                i += 1
    return starts


def pending_from(messages: Sequence[Message]) -> int:
    """Index where the suffix the model has NOT answered yet begins.

    Everything after the last assistant message that carries no tool call (a final
    answer) is pending: the user's current input, and any tool round still waiting
    for its continuation. With no such assistant message the whole history is
    pending. Pending messages are never summarised: a summary of the question the
    model is about to answer would delete the question.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and not _has_tool_call(message):
            return index + 1
    return 0


@dataclass(frozen=True)
class CompactionSplit:
    """``messages`` cut for compaction: summarise ``head``, keep ``tail`` verbatim.

    ``reason`` is ``"empty_head"`` when nothing precedes the part that may not be
    summarised (the history IS the unanswered input, or a single message), so there
    is nothing to compact; ``None`` otherwise.
    """

    head: list[Message]
    tail: list[Message]
    pending_from: int
    reason: str | None


def split_for_compaction(
    messages: Sequence[Message],
    *,
    tail_turns: int,
    tail_budget_tokens: int,
    size: Callable[[Sequence[Message]], int],
) -> CompactionSplit:
    """Cut ``messages`` into a head to summarise and a tail to keep.

    The tail starts at the ``tail_turns``-th most recent assistant message, as
    :func:`tail_split` does, and then:

    * never starts after the PENDING suffix (:func:`pending_from`): unanswered
      input is always in the tail;
    * only ever starts at an atomic unit boundary (:func:`unit_starts`);
    * is bounded by ``tail_budget_tokens``: while it is larger, its OLDEST unit moves
      into the head, down to the pending suffix and never past the newest unit (the
      shrink only advances to unit starts). The pending suffix itself can exceed the
      budget; it is kept regardless.

    With fewer assistant messages than ``tail_turns`` the turn-based tail is the whole
    history, so the budget alone decides what is summarised: an oversized history is
    no longer returned unchanged.
    """
    if tail_turns < 0:
        raise ValueError(f"tail_turns must be >= 0, got {tail_turns!r}")
    msgs = list(messages)
    n = len(msgs)
    starts = unit_starts(msgs)
    pending = pending_from(msgs)

    assistants = [i for i, m in enumerate(msgs) if m.role == "assistant"]
    if tail_turns == 0 or not assistants:
        turn_boundary = n
    elif len(assistants) < tail_turns:
        turn_boundary = 0
    else:
        turn_boundary = assistants[-tail_turns]
    start = min(turn_boundary, pending)

    # Advance only to unit starts at or before the pending suffix: the pending input, and
    # the newest unit (a unit start is never past the last one), are never summarised.
    for next_start in (u for u in starts if start < u <= pending):
        if size(msgs[start:]) <= tail_budget_tokens:
            break
        start = next_start

    head, tail = msgs[:start], msgs[start:]
    return CompactionSplit(
        head=head, tail=tail, pending_from=pending, reason=None if head else "empty_head",
    )


__all__ = ["CompactionSplit", "pending_from", "split_for_compaction", "tail_split", "unit_starts"]
