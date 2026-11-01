"""Per-session token accounting, folded from the record ledger.

The DONE records already carry a usage envelope, so they are the
ledger. Keeping a counter column on the row would add a second source
of truth that has to be reconciled on every rewind and compaction.

Folding the VISIBLE set instead means both come out right for free:
rewound turns stop counting, and a compaction folds the usage of the
turns it replaced along with their text. A counter column could express
neither without a compensating write.
"""

from __future__ import annotations

from dataclasses import dataclass

from primer.model.workspace_session import SessionMessageKind
from primer.session.replay import _COMPACTION, _REWIND, visible_records
from primer.session.terminals import CLOSES, TurnWindowScanner, is_graph_end, payload_of

_DONE = SessionMessageKind.DONE.value


@dataclass(frozen=True)
class SessionUsage:
    """Token totals, and the turn and model-call counts, for what is currently visible in a session.

    ``turns`` is the number of turns IN VIEW: the visible records that end a turn by the shared rule
    (:class:`primer.session.terminals.TurnWindowScanner`: a failed turn is one, however many error records it wrote), so it equals the trace's turn ordinals. A compaction folds the turns it
    replaced away and a rewind drops the rewound ones, so after either it is NOT the session's lifetime count (that is the
    row's ``turn_no``). A ``done`` followed by the ``cancelled`` of a Stop that landed after the model finished counts as two,
    exactly as the trace splits it into two windows.

    ``model_calls`` is the number of visible ``done`` records: every model call, tool rounds and delegated (subagent) runs
    included, a graph node's too, but not the graph's own end (:func:`primer.session.terminals.is_graph_end`: it ends the run, it calls no model).
    It is the population ``total_*`` and ``last_*`` are folded over (a ``done`` with no usage envelope still counts).
    Before 01a1138d this number was reported as ``turns``.
    """

    turns: int = 0
    model_calls: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_input_tokens: int = 0
    total_reasoning_tokens: int = 0


def session_usage(raw_lines: list[str]) -> SessionUsage:
    """Fold the visible records into a usage summary: tokens and model calls over the DONE records, turns over the turn ends."""
    turns = model_calls = 0
    last_in = last_out = 0
    tot_in = tot_out = tot_cached = tot_reasoning = 0
    scanner = TurnWindowScanner()
    for rec in visible_records(raw_lines):
        if scanner.feed(rec) == CLOSES:
            turns += 1
        if rec.get("kind") != _DONE or is_graph_end(rec):          # the graph's own end is a done, but not a model call
            continue
        model_calls += 1
        usage = payload_of(rec).get("usage")
        if not isinstance(usage, dict):
            continue  # a turn can terminate without a usage envelope
        last_in = usage.get("input_tokens", 0)
        last_out = usage.get("output_tokens", 0)
        tot_in += last_in
        tot_out += last_out
        tot_cached += usage.get("cached_input_tokens", 0)
        tot_reasoning += usage.get("reasoning_tokens", 0)
    return SessionUsage(
        turns=turns,
        model_calls=model_calls,
        last_input_tokens=last_in,
        last_output_tokens=last_out,
        total_input_tokens=tot_in,
        total_output_tokens=tot_out,
        total_cached_input_tokens=tot_cached,
        total_reasoning_tokens=tot_reasoning,
    )


class UsageFold:
    """:func:`session_usage` as state that goes on from where it stopped: feed it the lines appended since, in file order.

    ``visible_records`` is a walk in file order, so for a log that only gains lines the fold is additive; a compaction marker replaces everything
    before it (the fold starts over at the marker), and a rewind marker cannot be undone from totals: :meth:`feed` then returns False and
    the caller folds the whole log with :func:`session_usage`. ``consumed`` and ``guard`` are the reader's bookkeeping (bytes of the log folded
    so far and the last bytes of them, to tell that the log still starts with what was folded).
    """

    def __init__(self) -> None:
        self.consumed = 0
        self.guard = b""
        self._reset()

    def _reset(self) -> None:
        self.turns = self.model_calls = self.last_in = self.last_out = 0
        self.tot_in = self.tot_out = self.tot_cached = self.tot_reasoning = 0
        self.scanner = TurnWindowScanner()

    def feed(self, raw_lines: list[str]) -> bool:
        """Fold the lines; False when one of them is a rewind marker (the fold is then unusable)."""
        from primer.session.replay import _parse

        for rec in _parse(raw_lines):
            kind = rec.get("kind")
            if kind == _REWIND:
                return False
            if kind == _COMPACTION:
                self._reset()
            if self.scanner.feed(rec) == CLOSES:
                self.turns += 1
            if kind != _DONE or is_graph_end(rec):
                continue
            self.model_calls += 1
            usage = payload_of(rec).get("usage")
            if not isinstance(usage, dict):
                continue
            self.last_in = usage.get("input_tokens", 0)
            self.last_out = usage.get("output_tokens", 0)
            self.tot_in += self.last_in
            self.tot_out += self.last_out
            self.tot_cached += usage.get("cached_input_tokens", 0)
            self.tot_reasoning += usage.get("reasoning_tokens", 0)
        return True

    def usage(self) -> SessionUsage:
        return SessionUsage(
            turns=self.turns, model_calls=self.model_calls, last_input_tokens=self.last_in, last_output_tokens=self.last_out,
            total_input_tokens=self.tot_in, total_output_tokens=self.tot_out, total_cached_input_tokens=self.tot_cached,
            total_reasoning_tokens=self.tot_reasoning,
        )


__all__ = ["SessionUsage", "UsageFold", "session_usage"]
