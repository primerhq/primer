"""Tail-split helper for the compaction strategy.

Splits a chat history into ``(head, tail)`` so the compactor can
replace the head with a summary while keeping the most-recent N
assistant turns verbatim.
"""

from __future__ import annotations

from collections.abc import Sequence

from matrix.model.chat import Message


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


__all__ = ["tail_split"]
