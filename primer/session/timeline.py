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
from primer.session.terminals import CLOSES, COPY, TurnWindowScanner, closes_turn, is_session_terminal

_DONE = SessionMessageKind.DONE.value
_ERROR = SessionMessageKind.ERROR.value
_CANCELLED = SessionMessageKind.CANCELLED.value

_GRAPH_TRANSITION = SessionMessageKind.GRAPH_TRANSITION.value
_YIELDED_RECORD = SessionMessageKind.YIELDED.value

# Turn-log kind (primer/model/turn_log.py:26). Same wire value as the
# YIELDED record kind, but a different vocabulary: this one names a
# turn-log envelope event, not a messages.jsonl row.
_YIELDED = TurnLogKind.YIELDED.value

# The turn-log kinds that end a turn (the session's own run, not a graph node's), and the ones that open an envelope: a resume writes ``resumed`` and
# then ``started`` for ONE envelope, a fresh run writes ``started`` alone. ``turn_envelopes`` opens a new envelope at an own event whose kind is in
# _RESUME_EVENT_KINDS, except a ``started`` that directly follows an own ``resumed``; ``_waits`` dates a resume by the first of them. An ENVELOPE ends
# at a terminal or at the ``yielded`` of a park.
_RESUMED = TurnLogKind.RESUMED.value
_STARTED = TurnLogKind.STARTED.value
_TURN_LOG_TERMINALS = frozenset({
    TurnLogKind.COMPLETED.value,
    TurnLogKind.FAILED.value,
    TurnLogKind.CANCELLED.value,
})
_RESUME_EVENT_KINDS = frozenset({_RESUMED, _STARTED})
_ENVELOPE_ENDS = _TURN_LOG_TERMINALS | {_YIELDED}


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
    terminal: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "turn_no": turn_no,
        "terminal_seq": terminal.get("seq") if terminal is not None else None,
        "records": [r for r in records if r.get("seq") in visible_seqs],
    }


def turn_windows(message_lines: list[str]) -> list[dict[str, Any]]:
    """Split the log into one window per turn, in order.

    Each window is ``{"turn_no", "terminal_seq", "records"}``. A trailing
    run with no terminal is returned as the last (open) window.

    A window ends at the record :class:`primer.session.terminals.TurnWindowScanner` says ends it: a FAILED turn is one window, ended by the first
    error of the failure (``terminal_seq`` is its seq). The rest of the failure (dispatch's error with the same words, the release marker) is a
    copy: it ends nothing, and it is filed with the window it copies, together with whatever was written between that window's end and the copy,
    so the next turn's window does not open with the last turn's leftovers (ticket 01a11ca5).

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
    scanner = TurnWindowScanner()
    for rec in _parse_records(message_lines):
        verdict = scanner.feed(rec)
        if verdict == COPY and windows:
            windows[-1]["records"].extend(r for r in (*current, rec) if r.get("seq") in visible_seqs)
            current = []
            continue
        current.append(rec)
        if verdict == CLOSES:
            windows.append(_window(len(windows), current, visible_seqs, rec))
            current = []
    if current:
        windows.append(_window(len(windows), current, visible_seqs, None))
    return windows




def turn_envelopes(turn_log_lines: list[str]) -> list[list[dict[str, Any]]]:
    """Group turn-log events into envelopes, ascending by ``turn_no``, seq-ordered within.

    The turn log is observability data, not a contract: unparseable lines
    and events with no turn_no are skipped rather than raising.

    A ``turn_no`` can hold more than one envelope. A FAILED turn does not bump
    it (``SessionClaimAdapter.on_release`` bumps on success only), so the turn
    that follows (a message to the failed session reopens it) writes under the
    same one; a turn whose end entry never landed (a worker crash, a lost
    lease) leaves the same shape. Without a split the failed turn and its
    successor were one group whose last event is the successor's
    ``completed``, so the failed turn's trace read "completed" and every later
    window was served another turn's envelope (ticket 01a11ce4). A new
    envelope opens at an own event (no ``node_id``: a graph node's events never
    split anything) whose kind is in ``_RESUME_EVENT_KINDS``, except a
    ``started`` that directly follows an own ``resumed`` (a resume writes
    ``resumed`` and then ``started`` for ONE envelope). Whether two envelopes
    are ONE turn is :func:`envelopes_for_window`'s question.

    The node scoping covers the GROUPING (and :func:`_last_own_end`) only: a
    group's status, times and waits (:func:`_turn_status`, :func:`_started_at`,
    :func:`_ended_at`, :func:`_waits`) still read every event in it, a graph
    node's included.
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
        previous_own: str | None = None
        for event in sorted(by_turn[turn_no], key=lambda e: e.get("seq") or 0):
            kind = event.get("kind")
            own = event.get("node_id") is None
            if own and current and kind in _RESUME_EVENT_KINDS and not (kind == _STARTED and previous_own == _RESUMED):
                groups.append(current)
                current = []
            current.append(event)
            if own:
                previous_own = kind
        groups.append(current)
    return groups


def _last_own_end(group: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last event of ``group`` that ends the session's own envelope (not a ``phase``, not a graph node's event)."""
    return next(
        (e for e in reversed(group) if e.get("node_id") is None and e.get("kind") in _ENVELOPE_ENDS), None,
    )


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
    it. One logical turn therefore spans a run of envelopes: a group whose
    last END event (``phase`` events follow a ``yielded``, so not simply its
    last event) is ``yielded`` is continued by the next group, which carries a
    later ``turn_no``. A ``yielded`` followed by a ``resumed`` on the SAME
    ``turn_no`` is not that: ``abandon_session_gate`` clears the park but leaves
    ``parked_at`` set and writes no turn-log event and no release, so the next
    ``run_one_session_turn`` writes ``resumed`` on the ``turn_no`` the park
    never bumped. That continues the session as a new turn, so those are two
    runs.
    """
    runs: list[list[list[dict[str, Any]]]] = []
    current: list[list[dict[str, Any]]] = []
    for position, group in enumerate(groups):
        current.append(group)
        end = _last_own_end(group)
        following = groups[position + 1] if position + 1 < len(groups) else None
        continued = (
            end is not None and end.get("kind") == _YIELDED and following is not None
            and (following[0].get("turn_no") or 0) > (group[0].get("turn_no") or 0)
        )
        if not continued:
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
    calls: dict[tuple[str | None, str | None, str], dict[str, Any]],
    calls_by_raw_id: dict[str, dict[str, Any]],
    calls_by_run: dict[tuple[str | None, str | None, str], dict[str, Any]],
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

    In a graph that is still not exact: two fan-out siblings can delegate
    under the same raw id at once, both parent runs are ``None``, and the
    last call wins. A record that carries ``delegate_node_id`` (the node
    whose agent made the call) is looked up by it and ONLY by it: another
    node's call is the wrong answer, not a fallback, so a stamped record
    whose call is not there stays where an un-nestable record stays. A
    record without it (outside a graph, or from before the stamp) takes the
    last call as before (ticket 01a11cca).
    """
    delegate = payload.get("delegate_tool_call_id")
    if payload.get("delegated"):
        if payload.get("delegate_run_id") is not None:
            parent_run = payload.get("delegate_parent_run_id")
            node_id = payload.get("delegate_node_id")
            target = calls_by_run.get((parent_run, node_id or None, delegate))
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

    ``calls`` is keyed by ``(delegate_run_id, node_id, tool_call_id)``, not
    tool_call_id alone. The run is ``None`` for the parent turn's own calls;
    a delegated run numbers its scoped ids from a counter of its own, so its
    first call and the parent's (or another run's) share ``x:tool:1:1`` and
    a result would pair with whichever was registered last (ticket 01a11cd4).
    The node is there for a reason of its own (01a0518f defense-in-depth): the write side now mints a
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
    calls: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
    calls_by_raw_id: dict[str, dict[str, Any]] = {}
    # (the run that made the call, the graph node, raw id) -> entry: the run is ``None`` for a call the parent turn itself made, otherwise the
    # ``delegate_run_id`` the call's own record carries (it is a delegated record). Every call is registered under ``None`` for the node too (the
    # last call with that run and raw id wins, which is all a record without ``delegate_node_id`` can ask for) and, when it has one, under its node
    # (``delegate_node_id`` of the run that made it, else the record's own ``node_id``), so two fan-out siblings that delegate under one raw id
    # do not replace each other.
    calls_by_run: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
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
                calls[(payload.get("delegate_run_id"), rec.get("node_id"), payload["id"])] = entry
                # A record predating the raw_id field (01a0518f) has no
                # separate raw id at all - payload["id"] WAS the raw id
                # at write time, so falling back to it here is correct,
                # not just defensive.
                raw_id = payload.get("raw_id") or payload["id"]
                calls_by_raw_id[raw_id] = entry
                call_run = payload.get("delegate_run_id")
                calls_by_run[(call_run, None, raw_id)] = entry
                call_node = payload.get("delegate_node_id") or rec.get("node_id")
                if call_node:
                    calls_by_run[(call_run, call_node, raw_id)] = entry
        elif kind == _TOOL_RESULT:
            parent = calls.get((payload.get("delegate_run_id"), rec.get("node_id"), payload.get("call_id")))
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
            parent = calls.get((payload.get("delegate_run_id"), rec.get("node_id"), payload.get("call_id")))
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
