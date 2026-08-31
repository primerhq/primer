"""Turn-pairing bookkeeping ported from chat's terminal counting.

Chat's invariant (primer/chat/dispatch.py:684-751): every user message is
closed by exactly one terminal record, and the next turn is found by
COUNTING those pairs, never by reading a flag. Counting survives a worker
dying mid-turn, where a flag would strand the session.

On sessions the terminals are DONE / ERROR / CANCELLED. YIELDED is NOT
terminal: a parked turn is still open, and the resumed continuation
writes the closing record. The routing rule in the steer path keeps the
pairing 1:1 by turning a steer that arrives while a turn is open into a
PendingSessionMessage rather than a second USER_INPUT.

The log is dual-format: SessionMessageRecord dumps interleave with plain
role/parts Message lines (primer/workspace/session.py:113-161). Only the
record lines carry ``seq`` and ``kind``, so everything else is skipped,
along with any partially written line a crash may have left behind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from primer.model.workspace_session import SessionMessageKind

_TERMINAL_KINDS = frozenset({
    SessionMessageKind.DONE.value,
    SessionMessageKind.ERROR.value,
    SessionMessageKind.CANCELLED.value,
})


@dataclass
class TurnCount:
    """Tally of a scan window: opens, closes, and the high-water seq."""

    open_user_inputs: int
    terminals: int
    max_seen_seq: int


def count_turn_state(raw_lines: list[str], *, cursor: int) -> TurnCount:
    """Count non-excluded USER_INPUTs against terminals at seq >= cursor."""
    user_inputs = 0
    terminals = 0
    max_seq = cursor - 1
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "kind" not in obj or "seq" not in obj:
            continue  # role/parts Message line, or a foreign record
        seq = obj.get("seq")
        if not isinstance(seq, int) or seq < cursor:
            continue
        max_seq = max(max_seq, seq)
        kind = obj.get("kind")
        if kind == SessionMessageKind.USER_INPUT.value:
            if (obj.get("payload") or {}).get("_history_excluded"):
                continue
            user_inputs += 1
        elif kind in _TERMINAL_KINDS:
            terminals += 1
    return TurnCount(
        open_user_inputs=user_inputs, terminals=terminals, max_seen_seq=max_seq,
    )


def has_open_turn(raw_lines: list[str], cursor: int) -> bool:
    """True when a user message in the window has no closing terminal."""
    tc = count_turn_state(raw_lines, cursor=cursor)
    return tc.open_user_inputs > tc.terminals


__all__ = ["TurnCount", "count_turn_state", "has_open_turn"]
