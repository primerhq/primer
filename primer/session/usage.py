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

_DONE = SessionMessageKind.DONE.value


@dataclass(frozen=True)
class SessionUsage:
    """Token totals for what is currently visible in a session."""

    turns: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_input_tokens: int = 0
    total_reasoning_tokens: int = 0


def session_usage(raw_lines: list[str]) -> SessionUsage:
    """Fold the visible DONE records into a usage summary."""
    turns = 0
    last_in = last_out = 0
    tot_in = tot_out = tot_cached = tot_reasoning = 0
    for rec in visible_records(raw_lines):
        if rec.get("kind") != _DONE:
            continue
        turns += 1
        usage = (rec.get("payload") or {}).get("usage")
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
        last_input_tokens=last_in,
        last_output_tokens=last_out,
        total_input_tokens=tot_in,
        total_output_tokens=tot_out,
        total_cached_input_tokens=tot_cached,
        total_reasoning_tokens=tot_reasoning,
    )


__all__ = ["SessionUsage", "session_usage"]
