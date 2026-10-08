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
from primer.session.terminals import closes_turn, is_session_terminal

_DONE = SessionMessageKind.DONE.value
_ERROR = SessionMessageKind.ERROR.value
_CANCELLED = SessionMessageKind.CANCELLED.value

_GRAPH_TRANSITION = SessionMessageKind.GRAPH_TRANSITION.value
_YIELDED_RECORD = SessionMessageKind.YIELDED.value

# Turn-log kind (primer/model/turn_log.py:26). Same wire value as the
# YIELDED record kind, but a different vocabulary: this one names a
# turn-log envelope event, not a messages.jsonl row.
_YIELDED = TurnLogKind.YIELDED.value


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




# Turn-log kinds that end an envelope of the session's own turn, and the ones that begin the next. A park ends one (``yielded``) and its
# resume begins the next with ``resumed`` and then ``started``.
_ENVELOPE_ENDS = frozenset({
    TurnLogKind.COMPLETED.value, TurnLogKind.FAILED.value, TurnLogKind.CANCELLED.value, TurnLogKind.YIELDED.value,
})
_ENVELOPE_STARTS = frozenset({TurnLogKind.STARTED.value, TurnLogKind.RESUMED.value})


def turn_envelopes(turn_log_lines: list[str]) -> list[list[dict[str, Any]]]:
    """Group turn-log events into envelopes, ascending by ``turn_no``, seq-ordered within.

    The turn log is observability data, not a contract: unparseable lines
    and events with no turn_no are skipped rather than raising.

    A ``turn_no`` can hold more than one envelope: a FAILED turn does not bump
    it (``SessionClaimAdapter.on_release`` bumps on success only), so the turn
    that follows (a message to the failed session reopens it) writes under the
    same one. An envelope that has ended (completed, failed, cancelled or
    yielded) and meets a ``started`` or ``resumed`` of the session's own turn
    (no ``node_id``) closes there and the event opens the next. Without that
    the failed turn and its successor were one group whose last event is the
    successor's ``completed``, so the failed turn's trace read "completed" and
    every later window was served another turn's envelope (ticket 01a11ce4).
    A park and its resume on one ``turn_no`` split the same way and are put
    back together by :func:`envelopes_for_window` (a group that closes with
    ``yielded`` is continued by the next).
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
    groups: list[list[dict[str, Any]]] = []
    for turn_no in sorted(by_turn):
        current: list[dict[str, Any]] = []
        ended = False
        for event in sorted(by_turn[turn_no], key=lambda e: e.get("seq") or 0):
            own = event.get("node_id") is None
            if ended and own and event.get("kind") in _ENVELOPE_STARTS:
                groups.append(current)
                current, ended = [], False
            current.append(event)
            if own and event.get("kind") in _ENVELOPE_ENDS:
                ended = True
        groups.append(current)
    return groups


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

# Dogfood round 2: the trace overlay's expanded form shows a call's
# result alongside its arguments - a tool's output (a read's full file,
# a long command's stdout) can be arbitrarily large, and this is a debug
# view riding the timeline response, not the transcript's own paginated
# rendering, so it needs its own bound rather than inheriting one.
_RESULT_OUTPUT_CAP = 4000


def _capped_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Size-capped view of a TOOL_RESULT payload for the trace overlay."""
    output = payload.get("output")
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    return {
        "output": text[:_RESULT_OUTPUT_CAP],
        "error": bool(payload.get("error")),
        "truncated": len(text) > _RESULT_OUTPUT_CAP,
    }


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
    # Read from the last record, but only a terminal of the SESSION's own run says how the turn went: a subagent's
    # failed / cancelled / done (payload.delegated) is the end of the subagent's turn, and its parent carries on.
    last = records[-1] if records else None
    if last is not None and is_session_terminal(last):
        kind = last.get("kind")
        if kind == _ERROR:
            return "failed"
        if kind == _CANCELLED:
            return "cancelled"
        if kind == _DONE and closes_turn(last):
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
    calls: dict[tuple[str | None, str], dict[str, Any]],
    calls_by_raw_id: dict[str, dict[str, Any]],
    calls_by_run: dict[tuple[str | None, str], dict[str, Any]],
) -> None:
    """Place one child entry: delegation wins, then node, else the root.

    Delegated records (C1: an inline subagent run appends to the PARENT
    log) carry the delegating tool_call_id, so they nest under that call
    rather than sitting beside it. That id is always the RAW provider id
    (primer.session.delegation's DelegationRecorder stamps it from a
    completely separate _CoalesceState that never sees the delegating
    call's scoped-id minting - see persistence.py's ToolCallEnd handler),
    so this looks it up in ``calls_by_raw_id`` (raw id -> entry, no node
    scoping - the delegating call and the delegated records don't
    reliably share a node_id either, see _tree()'s docstring), NOT
    ``calls`` (node+scoped-id -> entry, for TOOL_RESULT/CLIENT_ACTION
    pairing).

    The raw id is not unique, and a raw-id lookup alone mis-nests when two
    runs reuse one (a child's own call reuses its parent's id and replaces
    the parent's entry in the map, so everything after it nests under the
    CHILD's call, and a grandchild's records under whichever entry came
    last). A record that carries ``delegate_run_id`` (everything written
    since the recorder stamps one) is therefore looked up by the run that
    MADE the delegating call, ``(delegate_parent_run_id, raw id)`` in
    ``calls_by_run``, which is exact; a record that is not found that way
    stays at the node or the root rather than under a wrong call. A record
    without a run id (written before) keeps the raw-id lookup.
    """
    delegate = payload.get("delegate_tool_call_id")
    if payload.get("delegated"):
        if payload.get("delegate_run_id") is not None:
            target = calls_by_run.get((payload.get("delegate_parent_run_id"), delegate))
        else:
            target = calls_by_raw_id.get(delegate)
        if target is not None:
            target["children"].append(entry)
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

    ``calls`` is keyed by ``(node_id, tool_call_id)``, not tool_call_id
    alone (01a0518f defense-in-depth): the write side now mints a
    node+turn+seq-scoped id for every NEW record (primer.session.
    persistence's _CoalesceState.scoped_call_ids), so a live collision
    shouldn't reach here at all - but a record persisted before that fix
    landed still carries the bare provider id, and two such records from
    concurrent fan-out siblings sharing one raw id would otherwise
    overwrite each other's ``calls`` entry / mispair a TOOL_RESULT to the
    wrong sibling's TOOL_CALL. ``calls_by_raw_id`` is a SEPARATE, bare
    (no node scoping) raw-id -> entry map used only by the delegation
    lookup in :func:`_attach` - see its docstring for why that lookup
    can't use the scoped/node-keyed ``calls`` map.
    """
    roots: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    calls: dict[tuple[str | None, str], dict[str, Any]] = {}
    calls_by_raw_id: dict[str, dict[str, Any]] = {}
    # (the run that made the call, raw id) -> entry: ``None`` for a call the parent turn itself made, otherwise the
    # ``delegate_run_id`` the call's own record carries (it is a delegated record).
    calls_by_run: dict[tuple[str | None, str], dict[str, Any]] = {}
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
                "arguments": payload.get("arguments") or {},
                "status": None,
                "duration_ms": None,
                "result": None,
                "children": [],
            }
            if payload.get("id"):
                calls[(rec.get("node_id"), payload["id"])] = entry
                # A record predating the raw_id field (01a0518f) has no
                # separate raw id at all - payload["id"] WAS the raw id
                # at write time, so falling back to it here is correct,
                # not just defensive.
                raw_id = payload.get("raw_id") or payload["id"]
                calls_by_raw_id[raw_id] = entry
                calls_by_run[(payload.get("delegate_run_id"), raw_id)] = entry
        elif kind == _TOOL_RESULT:
            parent = calls.get((rec.get("node_id"), payload.get("call_id")))
            if parent is not None:
                parent["status"] = "error" if payload.get("error") else "ok"
                parent["duration_ms"] = _delta_ms(
                    parent["ts"], rec.get("created_at"),
                )
                parent["result"] = _capped_result(payload)
            continue
        elif kind == _CLIENT_ACTION:
            # Task 13's leaf rule, preserved through this rewrite: the S3
            # delivery record belongs to the call it delivered, so it is
            # never routed through _attach.
            parent = calls.get((rec.get("node_id"), payload.get("call_id")))
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
        _attach(entry, rec, payload, roots, nodes, calls, calls_by_raw_id, calls_by_run)
    return roots


_RESUME_EVENT_KINDS = frozenset({
    TurnLogKind.RESUMED.value,
    TurnLogKind.STARTED.value,
})


def _waits(
    groups: list[list[dict[str, Any]]], records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Wait segments between a park and the continuation that resumed it.

    Primary source is the turn log: a group that closes with ``yielded``
    is followed by the group that continued it, whose first resumed (or
    started) event dates the resume. The record stream is the fallback for
    sessions running the Noop turn-log writer.
    """
    out: list[dict[str, Any]] = []
    for index, group in enumerate(groups[:-1]):
        yielded = next(
            (e for e in reversed(group) if e.get("kind") == _YIELDED), None,
        )
        if yielded is None:
            continue
        resumed = next(
            (e for e in groups[index + 1] if e.get("kind") in _RESUME_EVENT_KINDS),
            None,
        )
        if resumed is None:
            continue
        out.append({
            "from": yielded.get("ts"),
            "to": resumed.get("ts"),
            "ms": _delta_ms(yielded.get("ts"), resumed.get("ts")),
            "event_key": yielded.get("event_key"),
        })
    if out:
        return out
    for index, rec in enumerate(records[:-1]):
        if rec.get("kind") != _YIELDED_RECORD:
            continue
        nxt = records[index + 1]
        out.append({
            "from": rec.get("created_at"),
            "to": nxt.get("created_at"),
            "ms": _delta_ms(rec.get("created_at"), nxt.get("created_at")),
            "event_key": (rec.get("payload") or {}).get("event_key"),
        })
    return out


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
        "waits": _waits(groups, records),
        "children": _tree(records),
    }


__all__ = [
    "build_turn_timeline",
    "closes_turn",
    "envelopes_for_window",
    "turn_envelopes",
    "turn_windows",
]
