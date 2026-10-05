"""Context-overflow recovery pieces shared by the executor and the compaction strategy.

No I/O here:

* :func:`completed_rounds` and :func:`tool_rounds` say what a turn has finished: whole
  ``(assistant tool_use, tool results)`` rounds, never a call without its result;
* :class:`ReplayGuard` is the :class:`primer.agent.loop.PromptGuard` installed for the replay that
  follows a forced compaction: it reduces the tool results in the OUTGOING prompt (the S3 pruning
  primitive, forced) so a carried result that was itself the reason the call overflowed cannot
  overflow the replay too. Everything it reduces has ALREADY RUN, so its placeholders say so: the
  default ones tell the model to call the tool again, which would run a side effect twice;
* :func:`reduce_for_persist` is what a FAILED turn writes into the history instead of the raw
  results: the form the model last saw, cut further when it is still large. The raw output stays in
  the event log; persisting it raw would let the next turn overflow on it and wedge the session.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from primer.agent.prune import ALREADY_RAN_PLACEHOLDERS, PruneSet, prune_prompt
from primer.agent.tail import unit_starts
from primer.model.chat import Message, Tool, ToolCallPart, ToolResultPart

SizeOf = Callable[[Sequence[Message]], int]
ToolsSizeOf = Callable[[Sequence[Tool]], int]


def _has_tool_call(message: Message) -> bool:
    return message.role == "assistant" and any(p.type == "tool_call" for p in message.parts)


def completed_rounds(turn_messages: Sequence[Message]) -> list[Message]:
    """The part of a turn's own messages (after its input) that is whole tool rounds.

    The prefix that ends with the last tool message: every assistant message with tool calls in it
    is followed by its results. A trailing assistant message (the final reply the model was
    streaming, or a tool call whose dispatch never finished) is not a completed round and is left
    out, so a replay or a persisted failure never holds a tool call without its result.
    """
    last = max((i for i, m in enumerate(turn_messages) if m.role == "tool"), default=-1)
    return list(turn_messages[: last + 1])


def tool_rounds(messages: Sequence[Message]) -> int:
    """How many tool rounds ``messages`` hold: assistant messages that carry a tool call."""
    return sum(1 for m in messages if _has_tool_call(m))


class ReplayGuard:
    """Reduce the tool results of the outgoing prompt to a size target, before every call.

    Installed only for the replay after an overflow. Each call it re-applies what it pruned on
    earlier calls (the prompt of one turn is append-only, so a recorded prune set stays valid),
    measures the result, and when that is still over ``target_tokens`` prunes the excess with the
    forced mode: older results are replaced by a placeholder, then the largest remaining ones, the
    newest rounds included, are truncated. It never touches the caller's own messages, so a
    successful replay persists the raw results. It reduces what it can: a prompt that is over the
    target with nothing left to shed is sent as it is.

    The target is for everything the call carries: the messages (the rendered system prompt is one of
    them) AND the tool schemas, which go out on every call whatever the history is (``tools_size``).
    Counting only the messages let an agent with a large tool catalogue be reduced to a target the
    schemas then overflowed on their own.
    """

    def __init__(self, *, target_tokens: int, size: SizeOf, tools_size: ToolsSizeOf | None = None) -> None:
        self._target = target_tokens
        self._size = size
        self._tools_size = tools_size
        self._sticky = PruneSet()
        self.shed_calls = 0

    @property
    def prune_set(self) -> PruneSet:
        """What this guard has reduced so far (re-applied to the rounds a failed turn persists)."""
        return self._sticky

    async def before_call(self, prompt: list[Message], *, tools: list[Tool]) -> list[Message]:
        outcome = prune_prompt(prompt, sticky=self._sticky, placeholders=ALREADY_RAN_PLACEHOLDERS)
        schemas = self._tools_size(tools) if self._tools_size is not None and tools else 0
        over = self._size(outcome.messages) + schemas - self._target
        if over > 0:
            outcome = prune_prompt(
                prompt, sticky=self._sticky, shed_tokens=over, force=True, placeholders=ALREADY_RAN_PLACEHOLDERS,
            )
            self.shed_calls += 1
        self._sticky = outcome.prune_set
        return outcome.messages

    def after_call(self, usage) -> None:
        return None


def cap_newest_round(rounds: Sequence[Message], *, cap_tokens: int, size: SizeOf) -> list[Message]:
    """``rounds`` with the NEWEST round cut to ``cap_tokens``, the older ones untouched.

    The forced compaction that folds a turn's completed rounds protects the opening user input and the
    newest round: nothing can summarise them, so what is left for the prompt after the fixed part has
    to hold both. When the newest round alone is over ``cap_tokens`` it is reduced further (its results
    already ran: the placeholders say so), down to its placeholders when the cap is 0. The call/result
    envelope stays, so a tool call is never separated from its result.
    """
    starts = unit_starts(rounds)
    if not starts:
        return list(rounds)
    k = starts[-1]
    newest = rounds[k:]
    if size(newest) <= cap_tokens:
        return list(rounds)
    return [*rounds[:k], *reduce_for_persist(newest, sticky=PruneSet(), target_tokens=cap_tokens, size=size)]


def reduce_for_persist(
    rounds: Sequence[Message],
    *,
    sticky: PruneSet,
    target_tokens: int,
    size: SizeOf,
    context: Sequence[Message] = (),
) -> list[Message]:
    """The form of a failed turn's completed rounds that goes into the history.

    First what the model last saw (the replay guard's recorded reductions), then, when the rounds
    are still over ``target_tokens``, a forced cut of the largest results. The call/result
    envelope stays; the placeholders say the calls already ran.

    A recorded prune set keys a result by its call id, a hash of its output and the OCCURRENCE of
    that pair in the prompt it was recorded against, counted in prompt order. ``context`` is what
    came before ``rounds`` in that prompt (the history the replay was sent): the set is applied to
    ``context + rounds`` so the occurrences line up, and only the rounds are returned. Applied to the
    rounds alone, an identical pair earlier in the history shifts every index, and a recorded
    reduction misses its result (it is persisted raw) or lands on another one.
    """
    full = [*context, *rounds]
    outcome = prune_prompt(full, sticky=sticky, keep_rounds=0, placeholders=ALREADY_RAN_PLACEHOLDERS)
    reduced = outcome.messages[len(context):]
    over = size(reduced) - target_tokens
    if over > 0:
        # the cut is for the rounds alone: the context is history that is not being persisted
        reduced = prune_prompt(
            reduced, shed_tokens=over, keep_rounds=0, force=True, placeholders=ALREADY_RAN_PLACEHOLDERS,
        ).messages
    return reduced


def kept_rounds(compacted: Sequence[Message], rounds: Sequence[Message]) -> int:
    """How many of ``rounds`` (the newest units last) the compacted history still holds as messages.

    A compaction keeps the newest units whole and summarises the older ones, so what is kept is a
    suffix of ``rounds``: the units are compared from the end by the ids of the calls and results
    they hold.
    """
    def units(messages: Sequence[Message]) -> list[tuple[str, ...]]:
        starts = unit_starts(messages)
        return [
            tuple(p.id for m in messages[a:b] for p in m.parts if isinstance(p, (ToolCallPart, ToolResultPart)))
            for a, b in zip(starts, [*starts[1:], len(messages)])
        ]

    kept = 0
    for mine, theirs in zip(reversed(units(compacted)), reversed(units(rounds))):
        if mine != theirs or not mine:
            break
        kept += 1
    return kept


__all__ = [
    "ReplayGuard",
    "cap_newest_round",
    "completed_rounds",
    "kept_rounds",
    "reduce_for_persist",
    "tool_rounds",
]
