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
from datetime import datetime
from typing import Any

from primer.model.turn_log import TurnLogKind
from primer.model.workspace_session import SessionMessageKind
from primer.session.replay import visible_records

_DONE = SessionMessageKind.DONE.value
_ERROR = SessionMessageKind.ERROR.value
_CANCELLED = SessionMessageKind.CANCELLED.value

_TERMINAL_KINDS = frozenset({_DONE, _ERROR, _CANCELLED})
_GRAPH_TRANSITION = SessionMessageKind.GRAPH_TRANSITION.value

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


_LLM_CALL = SessionMessageKind.LLM_CALL.value
_TOOL_CALL = SessionMessageKind.TOOL_CALL.value
_TOOL_RESULT = SessionMessageKind.TOOL_RESULT.value
# S3's notifying-call delivery record, written into the same log one spec
# earlier. A leaf under the call it delivered, not a root child.
_CLIENT_ACTION = SessionMessageKind.CLIENT_ACTION.value

_TURN_LOG_TERMINALS = frozenset({
    TurnLogKind.COMPLETED.value,
    TurnLogKind.FAILED.value,
    TurnLogKind.CANCELLED.value,
})


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _delta_ms(start: Any, end: Any) -> int | None:
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


def _turn_status(
    events: list[dict[str, Any]], records: list[dict[str, Any]],
) -> str:
    for event in reversed(events):
        kind = event.get("kind")
        if kind == TurnLogKind.COMPLETED.value:
            return "completed"
        if kind == TurnLogKind.FAILED.value:
            return "failed"
        if kind == TurnLogKind.CANCELLED.value:
            return "cancelled"
        if kind == _YIELDED:
            return "parked"
    kind = records[-1].get("kind") if records else None
    if kind == _ERROR:
        return "failed"
    if kind == _CANCELLED:
        return "cancelled"
    if kind == _DONE and closes_turn(records[-1]):
        return "completed"
    return "running"


def _started_at(
    events: list[dict[str, Any]], records: list[dict[str, Any]],
) -> Any:
    for event in events:
        if event.get("kind") == TurnLogKind.STARTED.value and event.get("ts"):
            return event["ts"]
    return records[0].get("created_at") if records else None


def _ended_at(
    events: list[dict[str, Any]], records: list[dict[str, Any]],
) -> Any:
    for event in reversed(events):
        if event.get("kind") in _TURN_LOG_TERMINALS and event.get("ts"):
            return event["ts"]
    return records[-1].get("created_at") if records else None


def _attach(
    entry: dict[str, Any],
    rec: dict[str, Any],
    payload: dict[str, Any],
    roots: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    calls: dict[str, dict[str, Any]],
) -> None:
    """Place one child entry: delegation wins, then node, else the root.

    Delegated records (C1: an inline subagent run appends to the PARENT
    log) carry the delegating tool_call_id, so they nest under that call
    rather than sitting beside it.
    """
    delegate = payload.get("delegate_tool_call_id")
    if payload.get("delegated") and delegate in calls:
        calls[delegate]["children"].append(entry)
        return
    node = nodes.get(rec.get("node_id"))
    if node is not None:
        node["children"].append(entry)
        return
    roots.append(entry)


def _tree(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold a window's records into ordered child nodes.

    Three grouping rules, applied in order: a delegated record nests under
    the tool_call that delegated it, a node-attributed record nests under
    its graph node, everything else is a root child. tool_result rows are
    never children of their own: they close the tool_call they answer.
    """
    roots: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    calls: dict[str, dict[str, Any]] = {}
    for rec in records:
        kind = rec.get("kind")
        payload = rec.get("payload") or {}
        if kind == _GRAPH_TRANSITION:
            # persistence.py:310-321 writes the node id twice (payload and
            # record field); read the payload copy, fall back to the record.
            nid = payload.get("node_id") or rec.get("node_id")
            if payload.get("phase") == "enter":
                entry = {
                    "kind": "node",
                    "seq": rec.get("seq"),
                    "node_id": nid,
                    "node_kind": payload.get("node_kind"),
                    "started_at": rec.get("created_at"),
                    "ended_at": None,
                    "duration_ms": None,
                    "status": None,
                    "children": [],
                }
                nodes[nid] = entry
                roots.append(entry)
                continue
            open_node = nodes.get(nid)
            if open_node is not None:
                open_node["ended_at"] = rec.get("created_at")
                open_node["status"] = payload.get("status")
                open_node["duration_ms"] = _delta_ms(
                    open_node["started_at"], open_node["ended_at"],
                )
            continue
        if kind == _LLM_CALL:
            entry = {
                "kind": "llm_call",
                "seq": rec.get("seq"),
                "ts": rec.get("created_at"),
                "node_id": rec.get("node_id"),
                "profile_id": payload.get("profile_id"),
                "provider_id": payload.get("provider_id"),
                "model": payload.get("model"),
                "input_tokens": payload.get("input_tokens"),
                "output_tokens": payload.get("output_tokens"),
                "duration_ms": payload.get("duration_ms"),
                "status": payload.get("status"),
                "children": [],
            }
        elif kind == _TOOL_CALL:
            entry = {
                "kind": "tool_call",
                "seq": rec.get("seq"),
                "ts": rec.get("created_at"),
                "node_id": rec.get("node_id"),
                "tool_call_id": payload.get("id"),
                "name": payload.get("name"),
                "status": None,
                "duration_ms": None,
                "children": [],
            }
            if payload.get("id"):
                calls[payload["id"]] = entry
        elif kind == _TOOL_RESULT:
            parent = calls.get(payload.get("call_id"))
            if parent is not None:
                parent["status"] = "error" if payload.get("error") else "ok"
                parent["duration_ms"] = _delta_ms(
                    parent["ts"], rec.get("created_at"),
                )
            continue
        elif kind == _CLIENT_ACTION:
            # Task 13's leaf rule, preserved through this rewrite: the S3
            # delivery record belongs to the call it delivered, so it is
            # never routed through _attach.
            parent = calls.get(payload.get("call_id"))
            if parent is not None:
                parent["children"].append({
                    "kind": "client_action",
                    "seq": rec.get("seq"),
                    "ts": rec.get("created_at"),
                    "name": payload.get("name"),
                    "children": [],
                })
            continue
        else:
            continue
        _attach(entry, rec, payload, roots, nodes, calls)
    return roots


def build_turn_timeline(
    *,
    message_lines: list[str],
    turn_log_lines: list[str],
    turn_no: int,
) -> dict[str, Any] | None:
    """Fold one turn into its execution tree, or None if it does not exist.

    ``turn_no`` is the window ordinal from :func:`turn_windows`, counted
    by terminals over the unfolded record stream, and it selects the
    turn-log envelope RUN at the same ordinal. It is NOT an index into a
    folded list and it is NOT generally the envelope's own turn_no: a
    parked turn absorbs its continuation envelope.
    """
    windows = turn_windows(message_lines)
    if turn_no < 0 or turn_no >= len(windows):
        return None
    window = windows[turn_no]
    records = window["records"]
    groups = envelopes_for_window(turn_envelopes(turn_log_lines), turn_no)
    events = [event for group in groups for event in group]
    started_at = _started_at(events, records)
    ended_at = _ended_at(events, records)
    return {
        "turn_no": turn_no,
        "terminal_seq": window["terminal_seq"],
        "status": _turn_status(events, records),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": _delta_ms(started_at, ended_at),
        "waits": [],
        "children": _tree(records),
    }


__all__ = [
    "build_turn_timeline",
    "closes_turn",
    "envelopes_for_window",
    "turn_envelopes",
    "turn_windows",
]
