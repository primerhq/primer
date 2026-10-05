"""Making what the compaction's summariser reads fit its own window (A.4, R8).

The summariser is one more model call, and it is sent the head the compaction replaces. A head that is
too large for the model (the session was left over its window, or a forced compaction runs after an
overflow) makes THAT call overflow, and nothing summarises: the turn used to fail with the provider's
400. :func:`reduce_summary_input` shrinks the head in the order that loses the least, and says what it
did so the compaction marker can record it:

1. **Shed tool results**, largest first, to a placeholder that says the output was left out of the
   summariser's input (a summary of what a call returned matters less than the call, and the raw output
   is in the event log). The call/result envelope stays.
2. **Fold**, when that is still too large: the head is split at unit boundaries into at most
   :data:`MAX_CHUNKS` chunks of at most ``chunk_tokens`` each; the first is summarised, and each later call
   is sent the summary so far and the next chunk. Nothing is ever sent that does not fit.
3. **Cut a single unit** that alone is over a chunk: its largest part is cut head and tail (text, a
   tool result, a long string in a tool call's arguments), and media is replaced by a text placeholder.

Pure: no model call, no I/O. The caller (``CompactionStrategy._full_compact``) makes the calls.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from primer.agent.tail import unit_starts
from primer.model.chat import (
    DocumentPart,
    ExtendedPart,
    ImagePart,
    Message,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

SizeOf = Callable[[Sequence[Message]], int]
PartSizeOf = Callable[[Part], int]

#: At most this many chunks in the rolling fold: a head that needs more is not worth a chain of calls.
MAX_CHUNKS = 4
#: The share of the room the window leaves that a reduced input is sized to: the provider just counted more
#: than we did, so the input is not trimmed to the last token.
SUMMARISER_SAFETY = 0.8
#: A chunk of the rolling fold is at most this share of the compaction budget.
SUMMARISER_CHUNK_FRACTION = 0.5
#: When our estimate says the head fits and the call overflowed anyway, the provider counted more than we
#: did: the input is reduced to this share of what we count.
PROVIDER_COUNTED_MORE = 0.6
#: A tool result under this many tokens is not worth omitting.
MIN_RESULT_TOKENS = 100
#: A part is never cut below this many tokens.
FLOOR_PART_TOKENS = 50

OMITTED = "[output of {n} chars left out of the summariser's input: it did not fit the model's window]"
CUT = "\n[... {n} characters left out of the summariser's input: it did not fit the model's window ...]\n"
MEDIA = "[{kind} left out of the summariser's input: it did not fit the model's window]"


@dataclass(frozen=True)
class SummaryInputReduction:
    """What was done to the summariser's input, as the marker records it (``summary_input_reduced``)."""

    pruned: int = 0
    """Tool results omitted or cut."""
    folded_chunks: int = 0
    """Chunks of the rolling fold; ``0`` when one call was enough (a single chunk is one call)."""
    truncated_parts: int = 0
    """Parts cut head and tail, or media replaced by a placeholder."""
    tool_loop_cut_round: int = 0
    """A tool-enabled summariser's loop overflowed in this round (1-based) and ended with the summary it had written
    (it is never restarted); ``0`` when it did not. The head was not reduced then: the summary is whatever the earlier
    rounds wrote, which can be less than a summary of the head."""

    def as_payload(self) -> dict[str, int]:
        payload = {"pruned": self.pruned, "folded_chunks": self.folded_chunks, "truncated_parts": self.truncated_parts}
        if self.tool_loop_cut_round:
            payload["tool_loop_cut_round"] = self.tool_loop_cut_round
        return payload


@dataclass(frozen=True)
class ReducedInput:
    """The head, ready to send: ONE chunk is a single call, several are a rolling fold."""

    chunks: list[list[Message]]
    report: SummaryInputReduction


class SummaryInputUnreachable(Exception):
    """The head cannot be made to fit within :data:`MAX_CHUNKS` chunks (or nothing is left to cut)."""


@dataclass(frozen=True)
class SummariserSizing:
    """How much the summariser's retry may read: ``goal`` tokens when one call can read the head, in chunks of
    ``chunk_tokens`` (at most ``max_chunks`` of them) when it has to be folded."""

    goal: int
    chunk_tokens: int
    max_chunks: int


def size_summariser_input(
    *, window: int, budget: int, summary_tokens: int, frame: int, current: int, first_call_extra: int = 0,
    head_known_to_fit: bool = False,
    safety: float = SUMMARISER_SAFETY, chunk_fraction: float = SUMMARISER_CHUNK_FRACTION,
) -> SummariserSizing:
    """What a summariser whose call overflowed may be sent, from the model's ``window``, the compaction ``budget``
    (the window less the turn's output reserve), the ``summary_tokens`` it writes, the ``frame`` (its prompt and
    instruction), ``current``, our estimate of the head, and ``first_call_extra``, what the call that overflowed
    carried besides the head and its frame (the tool schemas of a tool-enabled summariser; the text-only retry drops
    them).

    The room is the WINDOW less what this call itself reserves for its output (the summary allowance, not the
    turn's larger reserve, which ``budget`` already took) and its frame. The retry is sized to ``safety`` of it. When
    OUR count says the call that overflowed did not fit (the head and ``first_call_extra`` over the room) the
    overflow is explained and the head is reduced to that; when it says the call fitted, the provider counted more
    than we did and the head is reduced to :data:`PROVIDER_COUNTED_MORE` of our count, however close to the room it
    is (a head just under the target must not be sent at 97% of what was rejected). ``head_known_to_fit``: an earlier
    round of a tool loop was accepted with the head and the schemas in it, so the head alone, text only, fits: it is
    never cut below its own size. A chunk of the fold is at most ``chunk_fraction`` of the budget,
    and leaves room for the summary so far, which every call after the first carries; when no chunk fits beside
    it the retry is ONE call (a bounded fold of one chunk). A chunk is never larger than ``goal``: a chunk the
    size of the head is the input that was just rejected. Raises :class:`SummaryInputUnreachable` when the window
    leaves no room for any input."""
    room = window - summary_tokens - frame
    target = int(safety * room)
    if target <= 0:
        raise SummaryInputUnreachable(
            f"the window of {window} tokens leaves no room for any input beside the summariser's own prompt "
            f"({frame}) and its summary (up to {summary_tokens})"
        )
    explained = current + first_call_extra > room
    goal = target if explained else int(PROVIDER_COUNTED_MORE * current)
    if head_known_to_fit:
        goal = max(goal, current)
    fold_chunk = min(int(chunk_fraction * budget), target - summary_tokens)
    chunk, max_chunks = (fold_chunk, MAX_CHUNKS) if fold_chunk > 0 else (target, 1)
    return SummariserSizing(goal=goal, chunk_tokens=min(chunk, goal), max_chunks=max_chunks)


def _text_chars(part: Part) -> int:
    if isinstance(part, TextPart):
        return len(part.text)
    if isinstance(part, ToolResultPart):
        return len(part.output)
    return 0


def _cut_text(text: str, keep_chars: int) -> str:
    """``text`` with its middle left out so that, marker included, it is about ``keep_chars`` long: head two
    thirds, tail one third. Unchanged when that would not make it shorter."""
    marker_len = len(CUT.format(n=len(text)))
    room = keep_chars - marker_len
    if room <= 0 or len(text) <= keep_chars:
        return text
    head = text[: room * 2 // 3]
    tail = text[len(text) - (room - len(head)):] if room - len(head) > 0 else ""
    cut = head + CUT.format(n=len(text) - len(head) - len(tail)) + tail
    return cut if len(cut) < len(text) else text


def _largest_string(value: Any, path: tuple = ()) -> tuple[int, tuple]:
    """The longest string leaf of a JSON-like value, as ``(length, path)``."""
    best: tuple[int, tuple] = (0, path)
    if isinstance(value, str):
        return len(value), path
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = list(enumerate(value))
    else:
        return best
    for key, child in items:
        found = _largest_string(child, (*path, key))
        if found[0] > best[0]:
            best = found
    return best


def _with_string(value: Any, path: tuple, new: str) -> Any:
    if not path:
        return new
    key, rest = path[0], path[1:]
    if isinstance(value, dict):
        return {**value, key: _with_string(value[key], rest, new)}
    out = list(value)
    out[key] = _with_string(out[key], rest, new)
    return out


def _shed_results(
    head: Sequence[Message], *, goal: int, size: SizeOf, part_size: PartSizeOf,
) -> tuple[list[Message], int]:
    """Omit tool results, biggest first (older first among equals), until the head weighs at most ``goal``."""
    messages = list(head)
    pruned = 0
    candidates = sorted(
        (
            (part_size(part), -mi, pi)
            for mi, msg in enumerate(messages) if msg.role == "tool"
            for pi, part in enumerate(msg.parts)
            if isinstance(part, ToolResultPart) and part_size(part) >= MIN_RESULT_TOKENS
        ),
        reverse=True,
    )
    for _tokens, neg_mi, pi in candidates:
        if size(messages) <= goal:
            return messages, pruned
        mi = -neg_mi
        part = messages[mi].parts[pi]
        assert isinstance(part, ToolResultPart)
        parts = list(messages[mi].parts)
        parts[pi] = part.model_copy(update={"output": OMITTED.format(n=len(part.output))})
        messages[mi] = messages[mi].model_copy(update={"parts": parts})
        pruned += 1
    return messages, pruned


def _shrink_part(part: Part, drop_tokens: int) -> Part:
    """``part`` with about ``drop_tokens`` taken out of its largest string (media becomes a text placeholder);
    the same part when it cannot be made smaller. The cut's own marker is inside what is kept, so the part
    really loses that much."""
    drop_chars = drop_tokens * 4
    if isinstance(part, TextPart):
        return part.model_copy(update={"text": _cut_text(part.text, max(FLOOR_PART_TOKENS * 4, len(part.text) - drop_chars))})
    if isinstance(part, ToolResultPart):
        keep = max(FLOOR_PART_TOKENS * 4, len(part.output) - drop_chars)
        return part.model_copy(update={"output": _cut_text(part.output, keep)})
    if isinstance(part, ToolCallPart):
        length, path = _largest_string(part.arguments)
        if path and length > FLOOR_PART_TOKENS * 4:
            value = part.arguments
            for key in path:
                value = value[key]
            keep = max(FLOOR_PART_TOKENS * 4, length - drop_chars)
            return part.model_copy(update={"arguments": _with_string(part.arguments, path, _cut_text(value, keep))})
        return part
    if isinstance(part, (ImagePart, DocumentPart, ExtendedPart)):
        kind = getattr(part.extended, "type", part.type) if isinstance(part, ExtendedPart) else part.type
        return TextPart(text=MEDIA.format(kind=kind))
    return part


def _shrinkable(part: Part, part_size: PartSizeOf) -> bool:
    if isinstance(part, (ImagePart, DocumentPart, ExtendedPart)):
        return True
    if isinstance(part, ToolCallPart):
        return _largest_string(part.arguments)[0] > FLOOR_PART_TOKENS * 4
    return _text_chars(part) > FLOOR_PART_TOKENS * 4


def _cut_unit(
    unit: Sequence[Message], *, cap: int, size: SizeOf, part_size: PartSizeOf,
) -> tuple[list[Message], int]:
    """A single unit cut, its largest part first, until it weighs at most ``cap``."""
    messages = list(unit)
    cut = 0
    stuck: set[tuple[int, int]] = set()
    while size(messages) > cap:
        located = [
            (part_size(part), mi, pi)
            for mi, msg in enumerate(messages) for pi, part in enumerate(msg.parts)
            if (mi, pi) not in stuck and _shrinkable(part, part_size)
        ]
        if not located:
            raise SummaryInputUnreachable("a single unit of the head cannot be cut small enough")
        _tokens, mi, pi = max(located)
        excess = size(messages) - cap
        parts = list(messages[mi].parts)
        # a margin under the cap: one cut is then enough, whatever the rounding adds
        shrunk = _shrink_part(parts[pi], excess + 16)
        if shrunk == parts[pi]:
            stuck.add((mi, pi))
            continue
        parts[pi] = shrunk
        messages[mi] = messages[mi].model_copy(update={"parts": parts})
        cut += 1
    return messages, cut


def reduce_summary_input(
    head: Sequence[Message],
    *,
    size: SizeOf,
    part_size: PartSizeOf,
    goal_tokens: int,
    chunk_tokens: int,
    max_chunks: int = MAX_CHUNKS,
) -> ReducedInput:
    """The head, reduced to what a summariser with ``goal_tokens`` of room can read, or in chunks of
    ``chunk_tokens`` for a rolling fold. Raises :class:`SummaryInputUnreachable` when even ``max_chunks``
    chunks (each unit cut to fit one) are not enough."""
    shed, pruned = _shed_results(head, goal=goal_tokens, size=size, part_size=part_size)
    if size(shed) <= goal_tokens:
        return ReducedInput(chunks=[shed], report=SummaryInputReduction(pruned=pruned))

    starts = unit_starts(shed)
    units = [shed[a:b] for a, b in zip(starts, [*starts[1:], len(shed)])]
    truncated = 0
    sized: list[list[Message]] = []
    for unit in units:
        if size(unit) > chunk_tokens:
            unit, cut = _cut_unit(unit, cap=chunk_tokens, size=size, part_size=part_size)
            truncated += cut
        sized.append(list(unit))

    chunks: list[list[Message]] = []
    current: list[Message] = []
    for unit in sized:
        if current and size([*current, *unit]) > chunk_tokens:
            chunks.append(current)
            current = []
        current.extend(unit)
    if current:
        chunks.append(current)
    if len(chunks) > max_chunks:
        raise SummaryInputUnreachable(
            f"the head needs {len(chunks)} chunks of at most {chunk_tokens} tokens; the fold is bounded at {max_chunks}"
        )
    return ReducedInput(
        chunks=chunks,
        # one chunk is one call, not a fold
        report=SummaryInputReduction(
            pruned=pruned, folded_chunks=len(chunks) if len(chunks) > 1 else 0, truncated_parts=truncated,
        ),
    )


__all__ = [
    "MAX_CHUNKS",
    "PROVIDER_COUNTED_MORE",
    "ReducedInput",
    "SUMMARISER_CHUNK_FRACTION",
    "SUMMARISER_SAFETY",
    "SummariserSizing",
    "SummaryInputReduction",
    "SummaryInputUnreachable",
    "reduce_summary_input",
    "size_summariser_input",
]
