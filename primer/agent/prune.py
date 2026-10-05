"""Pure, synchronous pruning of tool results in an outgoing prompt ("tier 1b").

Tool output is where a prompt grows fastest and where it is cheapest to give
back: the call/result envelope has to stay (an orphaned tool call is a provider
error), but the bytes of an old result usually do not. This module reduces tool
results in a prompt that is about to be sent. It does no I/O, takes no clock and
never mutates its input; it returns new messages and a record of what it did.

The one entry point is :func:`prune_prompt`. It always starts from the RAW
messages and a sticky :class:`PruneSet`, so the caller never has to reason about
already-reduced text:

* **Sticky.** A prune is identified by a position-stable key (:func:`result_keys`:
  tool-call id, a hash of the output, and the occurrence index of that exact pair
  among the prompt's tool results) and collected in a :class:`PruneSet`. A caller
  that records the set passes it back on every later prompt and the same results
  are reduced again, whatever any size signal says then. A prune that is
  re-derived from a size signal instead flips back off the moment the signal,
  measured on the already-pruned prompt, drops below the trigger, and the next
  call re-sends the raw history.
* **Position-stable.** Adapters mint ``call_0`` per stream, so the id alone is not
  unique; the hash alone is not either (a model that is told "call the tool again"
  and does so gets an identical output). The occurrence index tells the fresh
  repeat from the result that was pruned, so a recorded prune never lands on a
  fresh identical result. A result in the newest ``keep_rounds`` tool messages is
  also never replaced by a recorded placeholder.
* **Valid for one append-only raw sequence.** A key's occurrence index is counted
  in prompt order, so a recorded :class:`PruneSet` describes the raw prompt it was
  recorded against and its later appends. It is NOT valid across a history rewrite
  (a compaction, a rewind, anything that removes or inserts an earlier tool result
  with the same id and output): the indices shift, and a recorded key can then land
  on a different, fresh result. The caller must discard the set at any such
  rewrite. (Results with a distinct id or output are keyed apart and unaffected;
  the hazard is the identical pair.)
* **Sized by the caller.** How big a result is, is the caller's function
  (:data:`SizeFn`). The default is the character heuristic, which undercounts
  dense content (UUIDs, hashes, JSON numbers, base64) by up to ~2.75x; a caller
  with a real tokenizer passes it in. How much to give back is a token amount
  (``shed_tokens``), not a ratio scaled from a different content mix.
* **Envelope-preserving.** Only a result's ``output`` text is replaced. ``id``,
  ``error``, ``media`` and ``metadata`` stay, tool calls, system messages and user
  messages are untouched, and the newest ``keep_rounds`` tool messages are not
  replaced by a placeholder. ``force=True`` may still TRUNCATE (head and tail
  kept, the cut marked) an oversized result in those newest rounds, so a prompt
  whose bulk is its latest tool output can be reduced instead of reported
  unreducible. A truncation is recorded in the set like any prune and is
  re-applied wherever it lands, including once its round is no longer the newest
  (otherwise the raw result would return on the very next call).

"Already reduced" is decided by the :class:`PruneSet` the caller passes, never by
looking at the text of an output: a raw tool output that happens to contain the
placeholder wording is an ordinary output and can be pruned.

The placeholder text differs from the durable compaction tier's: an in-flight
output has no persisted history to consult, so it says to call the tool again.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from primer.model.chat import Message, ToolResultPart

SizeFn = Callable[[ToolResultPart], int]

DEFAULT_KEEP_ROUNDS = 2
DEFAULT_MIN_TOKENS = 1_000
DEFAULT_TRUNCATE_CHARS = 8_000

_OMITTED = "[output of {n} chars omitted to fit the context window; call the tool again if you need it]"
_CUT = "\n[... {n} chars omitted to fit the context window ...]\n"


def default_size(part: ToolResultPart) -> int:
    """The character heuristic's size of a result (undercounts dense content)."""
    return 20 + -(-len(part.output) // 4)


def result_keys(messages: Sequence[Message]) -> dict[tuple[int, int], str]:
    """A position-stable key for every tool result: ``id:hash#occurrence``.

    ``occurrence`` counts earlier results with the same id and output hash, in
    prompt order, so the first ``call_0`` returning X is ``...#0`` and a fresh
    ``call_0`` returning X later is ``...#1``.
    """
    seen: dict[str, int] = {}
    keys: dict[tuple[int, int], str] = {}
    for mi, msg in enumerate(messages):
        if msg.role != "tool":
            continue
        for pi, part in enumerate(msg.parts):
            if isinstance(part, ToolResultPart):
                digest = hashlib.sha256(part.output.encode("utf-8")).hexdigest()[:16]
                base = f"{part.id}:{digest}"
                keys[(mi, pi)] = f"{base}#{seen.get(base, 0)}"
                seen[base] = seen.get(base, 0) + 1
    return keys


@dataclass(frozen=True)
class PruneSet:
    """What has been pruned, in a form a caller can store and re-apply.

    ``omitted`` results are replaced by a placeholder; ``truncated`` maps a
    result to the number of characters kept (head 2/3, tail 1/3).

    Valid only for the raw sequence it was recorded against and its appends (see
    the module docstring): discard it on a compaction or any history rewrite.
    """

    omitted: frozenset[str] = frozenset()
    truncated: Mapping[str, int] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.omitted or self.truncated)

    def union(self, other: "PruneSet") -> "PruneSet":
        return PruneSet(
            omitted=self.omitted | other.omitted,
            truncated={**self.truncated, **other.truncated},
        )

    def to_payload(self) -> dict:
        """JSON-able form (for a record payload)."""
        return {"omitted": sorted(self.omitted), "truncated": dict(sorted(self.truncated.items()))}

    @classmethod
    def from_payload(cls, payload: Mapping | None) -> "PruneSet":
        if not payload:
            return cls()
        return cls(
            omitted=frozenset(payload.get("omitted") or ()),
            truncated={str(k): int(v) for k, v in (payload.get("truncated") or {}).items()},
        )


@dataclass(frozen=True)
class PruneOutcome:
    """The reduced messages, what was applied from the sticky set, and what is new.

    ``prune_set`` is the full effective set (applied plus added): the one to
    record and pass back next time. ``shed_by_sticky`` and ``shed_tokens`` are
    measured against the RAW messages (``shed_tokens`` includes the sticky part).
    """

    messages: list[Message]
    applied: PruneSet
    added: PruneSet
    shed_by_sticky: int
    shed_tokens: int

    @property
    def prune_set(self) -> PruneSet:
        return self.applied.union(self.added)


# The replay after a context overflow carries tool results that HAVE ALREADY RUN. The default
# placeholders tell the model to call the tool again, which would run its side effects a second time,
# so the replay (and a failed turn's persisted rounds) use these.
OMITTED_RAN = "[output of {n} chars omitted to fit the context window; this call ALREADY RAN, do NOT call it again]"
CUT_RAN = "\n[... {n} chars omitted to fit the context window; this call ALREADY RAN, do NOT call it again ...]\n"


@dataclass(frozen=True)
class Placeholders:
    """The text a reduced result is replaced by (``{n}`` is the number of characters left out)."""

    omitted: str = _OMITTED
    cut: str = _CUT


DEFAULT_PLACEHOLDERS = Placeholders()
ALREADY_RAN_PLACEHOLDERS = Placeholders(omitted=OMITTED_RAN, cut=CUT_RAN)


def _omit(part: ToolResultPart, placeholders: Placeholders = DEFAULT_PLACEHOLDERS) -> ToolResultPart:
    return part.model_copy(update={"output": placeholders.omitted.format(n=len(part.output))})


def _cut(
    part: ToolResultPart, keep_chars: int, placeholders: Placeholders = DEFAULT_PLACEHOLDERS,
) -> ToolResultPart:
    text = part.output
    if len(text) <= keep_chars:
        return part
    head = text[: keep_chars * 2 // 3]
    tail = text[len(text) - (keep_chars - len(head)):]
    return part.model_copy(update={
        "output": head + placeholders.cut.format(n=len(text) - len(head) - len(tail)) + tail,
    })


def _rewrite(
    messages: Sequence[Message],
    decisions: Mapping[tuple[int, int], ToolResultPart],
) -> list[Message]:
    """New messages with the decided replacement parts swapped in."""
    out: list[Message] = []
    for mi, msg in enumerate(messages):
        if not any((mi, pi) in decisions for pi in range(len(msg.parts))):
            out.append(msg)
            continue
        parts = [decisions.get((mi, pi), part) for pi, part in enumerate(msg.parts)]
        out.append(msg.model_copy(update={"parts": parts}))
    return out


def _total(messages: Sequence[Message], size: SizeFn) -> int:
    return sum(
        size(p) for m in messages if m.role == "tool"
        for p in m.parts if isinstance(p, ToolResultPart)
    )


def prune_prompt(
    messages: Sequence[Message],
    *,
    sticky: PruneSet = PruneSet(),
    shed_tokens: int = 0,
    keep_rounds: int = DEFAULT_KEEP_ROUNDS,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    force: bool = False,
    truncate_chars: int = DEFAULT_TRUNCATE_CHARS,
    size: SizeFn = default_size,
    placeholders: Placeholders = DEFAULT_PLACEHOLDERS,
) -> PruneOutcome:
    """Reduce tool results in the RAW ``messages``: re-apply ``sticky``, then give
    back about ``shed_tokens`` MORE, largest first.

    ``shed_tokens`` is the amount still to shed after the sticky set has been
    applied (the caller measures its signal on the sent, already-sticky prompt and
    passes the excess over its trigger). Phase 1 replaces results with the omitted
    placeholder, biggest first and older first among equals, skipping the newest
    ``keep_rounds`` tool messages, anything under ``min_tokens`` and anything the
    sticky set already reduced; it stops as soon as enough has been shed (no
    over-pruning). With ``force=True``, when that is not enough, phase 2 truncates
    the largest remaining results (the newest rounds included) to
    ``truncate_chars`` characters. It may shed less than asked when there is
    nothing left to reduce; the outcome reports what was actually shed. ``placeholders`` is the
    text a reduced result is replaced by (:data:`ALREADY_RAN_PLACEHOLDERS` for results that have
    executed and must not be called again); pass the same value on every call that re-applies a
    recorded set.
    """
    raw = list(messages)
    keys = result_keys(raw)
    tool_indices = [i for i, m in enumerate(raw) if m.role == "tool"]
    protected = set(tool_indices[max(0, len(tool_indices) - keep_rounds):]) if keep_rounds > 0 else set()

    decisions: dict[tuple[int, int], ToolResultPart] = {}
    applied_omit: set[str] = set()
    applied_cut: dict[str, int] = {}
    for (mi, pi), key in keys.items():
        part = raw[mi].parts[pi]
        if key in sticky.omitted and mi not in protected:
            decisions[(mi, pi)] = _omit(part, placeholders)  # type: ignore[arg-type]
            applied_omit.add(key)
        elif key in sticky.truncated:
            decisions[(mi, pi)] = _cut(part, sticky.truncated[key], placeholders)  # type: ignore[arg-type]
            applied_cut[key] = sticky.truncated[key]

    sticky_total = _total(raw, size) - _total(_rewrite(raw, decisions), size)
    shed = 0
    added_omit: set[str] = set()
    added_cut: dict[str, int] = {}

    if shed_tokens > 0:
        # (size, -message_index, part_index): biggest first, then older first.
        candidates: list[tuple[int, int, int]] = []
        for (mi, pi) in keys:
            if mi in protected or (mi, pi) in decisions:
                continue
            s = size(raw[mi].parts[pi])  # type: ignore[arg-type]
            if s >= min_tokens:
                candidates.append((s, -mi, pi))
        candidates.sort(reverse=True)
        for s, neg_mi, pi in candidates:
            if shed >= shed_tokens:
                break
            mi = -neg_mi
            part = raw[mi].parts[pi]
            saved = s - size(_omit(part, placeholders))  # type: ignore[arg-type]
            if saved <= 0:
                continue
            decisions[(mi, pi)] = _omit(part, placeholders)  # type: ignore[arg-type]
            added_omit.add(keys[(mi, pi)])
            shed += saved

        if force and shed < shed_tokens:
            cuts: list[tuple[int, int, int]] = []
            for (mi, pi) in keys:
                if (mi, pi) in decisions:
                    continue
                part = raw[mi].parts[pi]
                if len(part.output) > truncate_chars:  # type: ignore[union-attr]
                    cuts.append((size(part), -mi, pi))  # type: ignore[arg-type]
            cuts.sort(reverse=True)
            for s, neg_mi, pi in cuts:
                if shed >= shed_tokens:
                    break
                mi = -neg_mi
                part = raw[mi].parts[pi]
                saved = s - size(_cut(part, truncate_chars, placeholders))  # type: ignore[arg-type]
                if saved <= 0:
                    continue
                decisions[(mi, pi)] = _cut(part, truncate_chars, placeholders)  # type: ignore[arg-type]
                added_cut[keys[(mi, pi)]] = truncate_chars
                shed += saved

    out = _rewrite(raw, decisions)
    return PruneOutcome(
        messages=out,
        applied=PruneSet(omitted=frozenset(applied_omit), truncated=applied_cut),
        added=PruneSet(omitted=frozenset(added_omit), truncated=added_cut),
        shed_by_sticky=max(0, sticky_total),
        shed_tokens=max(0, _total(raw, size) - _total(out, size)),
    )


def apply_prune_set(
    messages: Sequence[Message],
    prune_set: PruneSet,
    *,
    keep_rounds: int = DEFAULT_KEEP_ROUNDS,
    size: SizeFn = default_size,
    placeholders: Placeholders = DEFAULT_PLACEHOLDERS,
) -> tuple[list[Message], int]:
    """Re-apply a recorded prune set and nothing else. Returns ``(messages, shed)``."""
    outcome = prune_prompt(messages, sticky=prune_set, keep_rounds=keep_rounds, size=size, placeholders=placeholders)
    return outcome.messages, outcome.shed_tokens


__all__ = [
    "ALREADY_RAN_PLACEHOLDERS",
    "DEFAULT_KEEP_ROUNDS",
    "DEFAULT_PLACEHOLDERS",
    "DEFAULT_MIN_TOKENS",
    "DEFAULT_TRUNCATE_CHARS",
    "PruneOutcome",
    "Placeholders",
    "PruneSet",
    "SizeFn",
    "apply_prune_set",
    "default_size",
    "prune_prompt",
    "result_keys",
]
