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
from primer.session.replay import visible_records
from primer.session.terminals import CLOSES, TurnWindowScanner, payload_of

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
    included. It is the population ``total_*`` and ``last_*`` are folded over (a ``done`` with no usage envelope still counts).
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
        if rec.get("kind") != _DONE:
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


__all__ = ["SessionUsage", "session_usage"]
