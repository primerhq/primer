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
from dataclasses import dataclass, field

from primer.model.chat import CompactionSummary, Message, ToolCallPart


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
    """Index where the CURRENT turn's unanswered part of ``messages`` begins.

    That is the run of user messages the model has not answered, together with the
    tool rounds that follow them (a turn the model is still working on, or one a park
    is about to resume): walk back over the trailing tool rounds (an assistant message
    that carries tool calls, and the tool messages that answer it), then over the run of
    user messages in front of them. Everything from there on is never summarised: a
    summary of the question the model is about to answer would delete the question.

    It is the CURRENT turn only. Tool rounds that belong to an EARLIER turn, one that
    ended without a final text reply (a Stop, the ``max_tool_turns`` cap, an empty
    completion), are not pending once a later user message follows them: they are
    ordinary history and summarisable, or a session that once ended that way could never
    be compacted again. A history that ends on a final assistant answer has nothing
    pending (the index is ``len(messages)``).

    A compaction summary that stands between the user run and the rounds is transparent. After a
    compaction that summarised the early rounds of the turn the history reads ``[question, summary,
    newest round]``; the summary is an assistant message, but it is not an answer to the question, so
    the walk goes on through it to the question, and a second compaction in the same turn still
    protects the question and folds the first summary into the second.
    """
    n = len(messages)
    i = n
    while i > 0 and (messages[i - 1].role == "tool" or _has_tool_call(messages[i - 1])):
        i -= 1
    if i < n:
        # a turn still in flight (it has rounds): look through the summaries in front of them
        j = i
        while j > 0 and isinstance(messages[j - 1], CompactionSummary):
            j -= 1
        if j > 0 and messages[j - 1].role == "user":
            i = j
    while i > 0 and messages[i - 1].role == "user":
        i -= 1
    return i


@dataclass(frozen=True)
class CompactionSplit:
    """``messages`` cut for compaction: summarise ``head``, keep ``tail`` verbatim.

    ``head`` is what the summary replaces, in order; it is a prefix of the history except when the
    current turn's EARLIER tool rounds are replaced too (see :func:`split_for_compaction`). ``tail``
    is what stays, in order. ``summary_after`` says where the summary goes in the compacted history:
    after that many kept messages (``0``, the usual, puts it in front; a turn whose early rounds are
    summarised puts it AFTER the user run that opened the turn, so the question stays first and
    verbatim). ``summary_input`` is what the summariser reads: everything up to the last message the
    summary replaces, the verbatim user run included, so the rounds are read with their question.

    ``reason`` is ``"empty_head"`` when there is nothing to replace, so there is nothing to compact;
    ``None`` otherwise.
    """

    head: list[Message]
    tail: list[Message]
    pending_from: int
    reason: str | None
    summary_after: int = 0
    summary_input: list[Message] = field(default_factory=list)


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

    * never starts after the PENDING suffix (:func:`pending_from`): the current turn's
      unanswered input is always kept;
    * only ever starts at an atomic unit boundary (:func:`unit_starts`);
    * is bounded by ``tail_budget_tokens``: while it is larger, its OLDEST unit moves
      into the head, down to the pending suffix and never past the newest unit (the
      shrink only advances to unit starts). ``tail_turns=0`` asks for no tail at all, so
      on an idle history it leaves nothing, the newest unit included.

    Inside the pending suffix (the user run that opened the current turn, then its tool rounds)
    only the opening user run and the NEWEST round are protected. Once the tail has shrunk to the
    pending suffix and is still over budget, the turn's EARLIER rounds are summarised too, oldest
    first, each as a whole unit: a single turn that reads many files can then be shrunk, which
    protecting every round of it could not. The summary goes after the user run
    (``summary_after``), so the compacted history reads ``[user run, summary, newest rounds]``.

    With fewer assistant messages than ``tail_turns`` the turn-based tail is the whole history, so
    the budget alone decides what is summarised. When that leaves NOTHING to replace (a history
    that fits its budget, on a path that asked to compact anyway) the tail shrinks to its floor, the
    user run plus the newest round (or the newest unit of an idle history), so there is something
    to summarise.

    ``reason`` is ``"empty_head"`` only when even the floor replaces nothing.

    ``size`` must be additive: the size of a list is the sum of the sizes of its messages, as the
    strategy's token estimate is. The in-turn shrink relies on it to keep a running total instead of
    measuring the whole tail again for every round it replaces.
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

    def cut(start: int, budget: int) -> int:
        """Advance ``start`` over unit starts, at or before the pending suffix, while the tail is over ``budget``.

        The newest unit is never summarised: a unit start is never past the last one."""
        for next_start in (u for u in starts if start < u <= pending):
            if size(msgs[start:]) <= budget:
                break
            start = next_start
        return start

    start = cut(start, tail_budget_tokens)
    if start == 0:
        # Nothing before the tail: shrink it to its floor so there is something to summarise.
        # A history that fits its budget is still compacted when compaction was asked for (the
        # force path); over the trigger it never gets here.
        start = cut(start, 0)

    # Inside the current turn: protect the opening user run and the newest round, summarise the
    # rounds between them, oldest first, while the tail is over budget (or there is nothing else
    # to replace). The summarised rounds are always one block, [user_end, removed_end), so the size
    # of what is kept is tracked as rounds leave it instead of measured again for every round.
    user_end = removed_end = start
    if start == pending and pending < n:
        user_end = removed_end = pending
        while user_end < n and msgs[user_end].role == "user":
            user_end += 1
        removed_end = user_end
        rounds = [(a, b) for a, b in zip(starts, [*starts[1:], n]) if a >= user_end]
        kept_tokens = size(msgs[start:])
        for a, b in rounds[:-1]:
            if kept_tokens <= tail_budget_tokens and (start > 0 or removed_end > user_end):
                break
            kept_tokens -= size(msgs[a:b])
            removed_end = b

    replaced = removed_end > user_end
    head = [*msgs[:start], *msgs[user_end:removed_end]]
    tail = [*msgs[start:user_end], *msgs[removed_end:]]
    return CompactionSplit(
        head=head, tail=tail, pending_from=pending, reason=None if head else "empty_head",
        summary_after=(user_end - start) if replaced else 0,
        summary_input=msgs[:removed_end if replaced else start],
    )


__all__ = ["CompactionSplit", "pending_from", "split_for_compaction", "tail_split", "unit_starts"]
