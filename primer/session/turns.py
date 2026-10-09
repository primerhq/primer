"""Turn-pairing bookkeeping ported from chat's terminal counting.

Chat's invariant (primer/chat/dispatch.py:684-751): every user message is
closed by exactly one terminal record, and the next turn is found by
COUNTING those pairs, never by reading a flag. Counting survives a worker
dying mid-turn, where a flag would strand the session.

On sessions the terminals are the DONE / ERROR / CANCELLED that ``terminals.closes_turn`` accepts: a model call that
ended in a tool call (``done(tool_use)``) ends a round, and a subagent's terminal (``payload.delegated``) ends the
subagent's turn, so neither closes the user's. A failed turn is one terminal however many error records it wrote (dispatch's error with the
same words and the release marker are copies, ``terminals.TurnWindowScanner``, ticket 01a11ca5). YIELDED is NOT terminal: a parked turn is still open, and the resumed
continuation writes the closing record. The routing rule in the steer path keeps the
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
from primer.session.terminals import CLOSES, TurnWindowScanner


@dataclass
class TurnCount:
    """Tally of a scan window: the input and terminal TOTALS, the turns still open, and the high-water seq.

    ``open_turns`` is what decides whether a turn is open, and it is order-aware: each terminal closes one input that came BEFORE it, so a terminal
    at the front of the window (the claim adapter's release marker is written at the seq the failure exit moved the cursor to) closes nothing and
    cannot cancel out an input that arrived after it. ``open_user_inputs - terminals`` is not that: it equals ``open_turns`` exactly when every
    terminal has an open input before it to close.
    """

    open_user_inputs: int
    terminals: int
    max_seen_seq: int
    open_turns: int


def count_turn_state(raw_lines: list[str], *, cursor: int) -> TurnCount:
    """Count non-excluded USER_INPUTs against terminals at seq >= cursor, in the order the log holds them."""
    user_inputs = 0
    terminals = 0
    open_turns = 0
    max_seq = cursor - 1
    scanner = TurnWindowScanner()
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
        verdict = scanner.feed(obj)          # every record, a history-excluded input included: it starts a turn for the failure fold
        if kind == SessionMessageKind.USER_INPUT.value:
            if (obj.get("payload") or {}).get("_history_excluded"):
                continue
            user_inputs += 1
            open_turns += 1
        elif verdict == CLOSES:
            terminals += 1
            open_turns = max(open_turns - 1, 0)   # a terminal closes an input before it; with none open it closes nothing
    return TurnCount(
        open_user_inputs=user_inputs, terminals=terminals, max_seen_seq=max_seq, open_turns=open_turns,
    )


def has_open_turn(raw_lines: list[str], cursor: int) -> bool:
    """True when a user message in the window has no closing terminal after it."""
    return count_turn_state(raw_lines, cursor=cursor).open_turns > 0


__all__ = ["TurnCount", "count_turn_state", "has_open_turn"]
