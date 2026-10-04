"""Pure, synchronous pruning of tool results in an outgoing prompt ("tier 1b").

Tool output is where a prompt grows fastest and where it is cheapest to give
back: the call/result envelope has to stay (an orphaned tool call is a provider
error), but the bytes of an old result usually do not. This module reduces tool
results in a prompt that is about to be sent. It does no I/O, takes no clock and
never mutates its input; it returns new messages and a record of what it did.

Three properties are the point:

* **Sticky.** A prune is identified by :func:`prune_key` (the tool-call id plus a
  hash of the output) and collected in a :class:`PruneSet`. A caller that records
  the set can re-apply it with :func:`apply_prune_set` on every later prompt,
  whatever any size signal says then. A prune that is re-derived from a size
  signal instead flips back off the moment the signal, measured on the already
  pruned prompt, drops below the trigger, and the next call re-sends the raw
  history. (The tool-call ids alone are not unique: adapters mint ``call_0`` per
  stream, so the hash is part of the key.)
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
  unreducible.

The placeholder text differs from the durable compaction tier's: an in-flight
output has no persisted history to consult, so it says to call the tool again.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from primer.model.chat import Message, ToolResultPart

SizeFn = Callable[[ToolResultPart], int]

DEFAULT_KEEP_ROUNDS = 2
DEFAULT_MIN_TOKENS = 1_000
DEFAULT_TRUNCATE_CHARS = 8_000

_OMITTED = "[output of {n} chars omitted to fit the context window; call the tool again if you need it]"
_OMITTED_RE = re.compile(r"^\[output of \d+ chars omitted to fit the context window;")
_CUT = "\n[... {n} chars omitted to fit the context window ...]\n"
_CUT_RE = re.compile(r"\[\.\.\. \d+ chars omitted to fit the context window \.\.\.\]")


def prune_key(part: ToolResultPart) -> str:
    """A stable identity for one tool result: id plus a hash of its output."""
    digest = hashlib.sha256(part.output.encode("utf-8")).hexdigest()[:16]
    return f"{part.id}:{digest}"


def default_size(part: ToolResultPart) -> int:
    """The character heuristic's size of a result (undercounts dense content)."""
    return 20 + -(-len(part.output) // 4)


def _is_reduced(part: ToolResultPart) -> bool:
    return bool(_OMITTED_RE.match(part.output) or _CUT_RE.search(part.output))


@dataclass(frozen=True)
class PruneSet:
    """What has been pruned, in a form a caller can store and re-apply.

    ``omitted`` results are replaced by a placeholder; ``truncated`` maps a
    result to the number of characters kept (head 2/3, tail 1/3).
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
    """The reduced messages and what this call added to them."""

    messages: list[Message]
    added: PruneSet
    shed_tokens: int


def _omit(part: ToolResultPart) -> ToolResultPart:
    return part.model_copy(update={"output": _OMITTED.format(n=len(part.output))})


def _cut(part: ToolResultPart, keep_chars: int) -> ToolResultPart:
    text = part.output
    if len(text) <= keep_chars:
        return part
    head = text[: keep_chars * 2 // 3]
    tail = text[len(text) - (keep_chars - len(head)):]
    omitted = len(text) - len(head) - len(tail)
    return part.model_copy(update={"output": head + _CUT.format(n=omitted) + tail})


def _rewrite(
    messages: Sequence[Message],
    replace: Callable[[int, int, ToolResultPart], ToolResultPart | None],
) -> list[Message]:
    """New messages with ``replace(mi, pi, part)`` applied to tool results."""
    out: list[Message] = []
    for mi, msg in enumerate(messages):
        if msg.role != "tool":
            out.append(msg)
            continue
        changed = False
        parts = []
        for pi, part in enumerate(msg.parts):
            new = replace(mi, pi, part) if isinstance(part, ToolResultPart) else None
            if new is not None and new is not part:
                changed = True
                parts.append(new)
            else:
                parts.append(part)
        out.append(msg.model_copy(update={"parts": parts}) if changed else msg)
    return out


def _total(messages: Sequence[Message], size: SizeFn) -> int:
    return sum(
        size(p) for m in messages if m.role == "tool"
        for p in m.parts if isinstance(p, ToolResultPart)
    )


def apply_prune_set(
    messages: Sequence[Message],
    prune_set: PruneSet,
    *,
    size: SizeFn = default_size,
) -> tuple[list[Message], int]:
    """Re-apply a recorded prune set. Returns ``(messages, tokens shed)``.

    A result whose output has changed since (a different hash) is not touched:
    the key names the exact output that was pruned.
    """
    if not prune_set:
        return list(messages), 0
    before = _total(messages, size)

    def replace(_mi: int, _pi: int, part: ToolResultPart) -> ToolResultPart | None:
        key = prune_key(part)
        if key in prune_set.omitted:
            return _omit(part)
        keep = prune_set.truncated.get(key)
        if keep is not None:
            return _cut(part, keep)
        return None

    out = _rewrite(messages, replace)
    return out, max(0, before - _total(out, size))


def prune_to_shed(
    messages: Sequence[Message],
    *,
    shed_tokens: int,
    keep_rounds: int = DEFAULT_KEEP_ROUNDS,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    force: bool = False,
    truncate_chars: int = DEFAULT_TRUNCATE_CHARS,
    size: SizeFn = default_size,
) -> PruneOutcome:
    """Give back about ``shed_tokens`` by reducing tool results, largest first.

    Phase 1 replaces results, oldest rounds first among equals and biggest first
    overall, with the omitted placeholder, skipping the newest ``keep_rounds`` tool
    messages, anything under ``min_tokens`` and anything already reduced. It stops
    as soon as enough has been shed (no over-pruning). With ``force=True``, when
    that is not enough, phase 2 truncates the largest remaining results (the newest
    rounds included) to ``truncate_chars`` characters. The result may shed less
    than asked when there is nothing left to reduce; ``shed_tokens`` on the outcome
    is what was actually shed.
    """
    if shed_tokens <= 0:
        return PruneOutcome(list(messages), PruneSet(), 0)

    tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    protected = set(tool_indices[len(tool_indices) - keep_rounds:]) if keep_rounds > 0 else set()

    # (size, -message_index, part_index): biggest first, then older first.
    omit_candidates: list[tuple[int, int, int]] = []
    for mi in tool_indices:
        if mi in protected:
            continue
        for pi, part in enumerate(messages[mi].parts):
            if isinstance(part, ToolResultPart) and not _is_reduced(part):
                s = size(part)
                if s >= min_tokens:
                    omit_candidates.append((s, -mi, pi))
    omit_candidates.sort(reverse=True)

    shed = 0
    chosen_omit: set[tuple[int, int]] = set()
    for s, neg_mi, pi in omit_candidates:
        if shed >= shed_tokens:
            break
        part = messages[-neg_mi].parts[pi]
        saved = s - size(_omit(part))  # type: ignore[arg-type]
        if saved <= 0:
            continue
        chosen_omit.add((-neg_mi, pi))
        shed += saved

    chosen_cut: dict[tuple[int, int], int] = {}
    if force and shed < shed_tokens:
        cut_candidates: list[tuple[int, int, int]] = []
        for mi in tool_indices:
            for pi, part in enumerate(messages[mi].parts):
                if (
                    isinstance(part, ToolResultPart)
                    and (mi, pi) not in chosen_omit
                    and not _is_reduced(part)
                    and len(part.output) > truncate_chars
                ):
                    cut_candidates.append((size(part), -mi, pi))
        cut_candidates.sort(reverse=True)
        for s, neg_mi, pi in cut_candidates:
            if shed >= shed_tokens:
                break
            part = messages[-neg_mi].parts[pi]
            saved = s - size(_cut(part, truncate_chars))  # type: ignore[arg-type]
            if saved <= 0:
                continue
            chosen_cut[(-neg_mi, pi)] = truncate_chars
            shed += saved

    if not chosen_omit and not chosen_cut:
        return PruneOutcome(list(messages), PruneSet(), 0)

    omitted_keys: set[str] = set()
    truncated_keys: dict[str, int] = {}

    def replace(mi: int, pi: int, part: ToolResultPart) -> ToolResultPart | None:
        if (mi, pi) in chosen_omit:
            omitted_keys.add(prune_key(part))
            return _omit(part)
        keep = chosen_cut.get((mi, pi))
        if keep is not None:
            truncated_keys[prune_key(part)] = keep
            return _cut(part, keep)
        return None

    out = _rewrite(messages, replace)
    return PruneOutcome(
        messages=out,
        added=PruneSet(omitted=frozenset(omitted_keys), truncated=truncated_keys),
        shed_tokens=max(0, _total(messages, size) - _total(out, size)),
    )


__all__ = [
    "DEFAULT_KEEP_ROUNDS",
    "DEFAULT_MIN_TOKENS",
    "DEFAULT_TRUNCATE_CHARS",
    "PruneOutcome",
    "PruneSet",
    "SizeFn",
    "apply_prune_set",
    "default_size",
    "prune_key",
    "prune_to_shed",
]
