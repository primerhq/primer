"""Read-side derivation of a turn's execution tree (12-s7-design.md section 6).

No trace system is built: the session's on-disk record IS the execution
trace. messages.jsonl supplies the child records, turns.jsonl supplies the
turn envelope (lifecycle timing, wait segments), and this module folds the
two into a tree. Pure derivation, no new write path, works on any
historical session.

Windows come from terminal counting over EVERY parsed record, because
the turn log they are joined to is never folded; each window's CONTENTS
are then filtered to what :func:`primer.session.replay.visible_records`
still shows, so a compaction or a rewind folds the trace exactly the way
it folds the transcript without renumbering the turns.
"""

from __future__ import annotations

import json
from typing import Any

from primer.model.turn_log import TurnLogKind
from primer.model.workspace_session import SessionMessageKind
from primer.session.replay import visible_records

_DONE = SessionMessageKind.DONE.value
_ERROR = SessionMessageKind.ERROR.value
_CANCELLED = SessionMessageKind.CANCELLED.value

_TERMINAL_KINDS = frozenset({_DONE, _ERROR, _CANCELLED})

# Turn-log kind (primer/model/turn_log.py:26). Same wire value as the
# YIELDED record kind, but a different vocabulary: this one names a
# turn-log envelope event, not a messages.jsonl row.
_YIELDED = TurnLogKind.YIELDED.value


def closes_turn(rec: dict[str, Any]) -> bool:
    """True when ``rec`` ends a turn rather than a tool round.

    The agent loop issues one ``llm.stream`` call per tool round and every
    stream ends with its own ``Done`` (primer/agent/loop.py), so an
    intermediate round produces a DONE record carrying
    ``stop_reason="tool_use"``. Counting those as terminals would split
    one turn into several windows.
    """
    kind = rec.get("kind")
    if kind not in _TERMINAL_KINDS:
        return False
    if kind == _DONE:
        return (rec.get("payload") or {}).get("stop_reason") != "tool_use"
    return True


def _parse_records(message_lines: list[str]) -> list[dict[str, Any]]:
    """Every event-log record in file order, folded or not.

    Same parse rule as :mod:`primer.session.replay` (a record is a dict
    carrying both ``kind`` and ``seq``), so plain role/parts message lines
    and half-written crash tails are skipped.
    """
    out: list[dict[str, Any]] = []
    for line in message_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "kind" in obj and "seq" in obj:
            out.append(obj)
    return out


def _window(
    turn_no: int,
    records: list[dict[str, Any]],
    visible_seqs: set[int],
) -> dict[str, Any]:
    terminal = records[-1] if records and closes_turn(records[-1]) else None
    return {
        "turn_no": turn_no,
        "terminal_seq": terminal.get("seq") if terminal is not None else None,
        "records": [r for r in records if r.get("seq") in visible_seqs],
    }


def turn_windows(message_lines: list[str]) -> list[dict[str, Any]]:
    """Split the log into one window per turn, in order.

    Each window is ``{"turn_no", "terminal_seq", "records"}``. A trailing
    run with no terminal is returned as the last (open) window.

    Turns are COUNTED over every parsed record, never over the visible
    ones: :func:`primer.session.replay.visible_records` replaces the whole
    visible set with a compaction marker and drops rewound rows, so the
    visible window count shrinks while turns.jsonl stays unfolded, and the
    ordinal join in :func:`envelopes_for_window` would then serve one
    turn's tree from another turn's envelope. Window CONTENTS are filtered
    to the visible set instead, so a fully folded turn keeps its ordinal
    and renders empty (12-s7-design.md section 6, crosscheck m4).
    """
    visible_seqs = {
        rec["seq"]
        for rec in visible_records(message_lines)
        if isinstance(rec.get("seq"), int)
    }
    windows: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for rec in _parse_records(message_lines):
        current.append(rec)
        if closes_turn(rec):
            windows.append(_window(len(windows), current, visible_seqs))
            current = []
    if current:
        windows.append(_window(len(windows), current, visible_seqs))
    return windows




def turn_envelopes(turn_log_lines: list[str]) -> list[list[dict[str, Any]]]:
    """Group turn-log events by ``turn_no``, ascending, seq-ordered within.

    The turn log is observability data, not a contract: unparseable lines
    and events with no turn_no are skipped rather than raising.
    """
    by_turn: dict[int, list[dict[str, Any]]] = {}
    for line in turn_log_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "kind" not in obj:
            continue
        turn_no = obj.get("turn_no")
        if not isinstance(turn_no, int):
            continue
        by_turn.setdefault(turn_no, []).append(obj)
    return [
        sorted(by_turn[turn_no], key=lambda e: e.get("seq") or 0)
        for turn_no in sorted(by_turn)
    ]


def envelopes_for_window(
    groups: list[list[dict[str, Any]]], index: int,
) -> list[list[dict[str, Any]]]:
    """Return the envelope groups belonging to window ``index``.

    ``index`` is the window's ``turn_no`` from :func:`turn_windows`, which
    counts terminals over the UNFOLDED record stream: the turn log is
    never folded, so a folded window count on one side of this join would
    serve one turn's tree from another turn's envelope.

    A park leaves turn_no untouched (the park branch of
    primer/claim/adapters/sessions.py returns before the bump) while the
    resume injection releases with success and no park, which DOES bump
    it. One logical turn therefore spans a run of envelopes: every group
    that closes with ``yielded`` is continued by the next one.
    """
    runs: list[list[list[dict[str, Any]]]] = []
    current: list[list[dict[str, Any]]] = []
    for group in groups:
        current.append(group)
        if (group[-1].get("kind") if group else None) != _YIELDED:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    if index < 0 or index >= len(runs):
        return []
    return runs[index]


__all__ = [
    "closes_turn",
    "envelopes_for_window",
    "turn_envelopes",
    "turn_windows",
]
