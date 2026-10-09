"""Two-phase turn rendering, plain-language tool chips, nested subagents.

Section 8 makes three things contract: a finished turn reads as a list of
answers, a tool chip never shows raw args, and subagent turns NEST under
the delegating turn keyed on the attribution S1 writes (crosscheck C1;
S1 plan pinned decision 5 names the payload key delegate_tool_call_id).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-turns.js"
ADAPTER = ROOT / "ui" / "components" / "session-adapter.jsx"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval((ROOT / "ui" / "foundation" / "shell-status.js").read_text(encoding="utf-8"))
    # UX reconcile wave 7: SH_toolChipLabel's result-aware branch calls
    # window.SA_resultCountLabel / window.SA_diffStatOfResult (session-
    # adapter.jsx) - only reached when a resultRow is passed, so the
    # pre-wave-7 single-arg tests below still pass without this, but the
    # two-arg tests need it loaded. session-adapter.jsx has no JSX in it
    # (see its own docstring: pure record->transcript mapping only), so
    # it evals here the same as in test_session_adapter.py's own harness.
    ctx.eval(ADAPTER.read_text(encoding="utf-8"))
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _windows_js(records_json: str) -> str:
    """The JS expression for the window map the console feeds SH_turnOfSeq: SH_windowsOfSeq over ALL the raw records (the server counts every
    record, the rewound ones included; ``SA_visibleRecords`` would drop them and number every turn after a rewind too low)."""
    return "SH_windowsOfSeq(" + records_json + ")"


def _server_windows(records: list[dict]) -> dict[int, int]:
    """seq -> the window the REAL ``turn_windows`` files the record in (not a copy of its rule)."""
    from primer.session.timeline import turn_windows

    windows = turn_windows([json.dumps(rec) for rec in records])
    return {rec["seq"]: window["turn_no"] for window in windows for rec in window["records"]}


def test_tool_chips_speak_plain_language() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toolChipLabel({
          kind: "tool_call",
          payload: {name: "workspace__grep",
                    arguments: {pattern: "webhook", path: "src/"}}
        }))
        """
    ))
    assert out["label"] == "searched src/"
    assert out["tone"] == "read"


def test_writes_are_prominent_and_reads_are_subdued() -> None:
    ctx = _ctx()
    write = json.loads(ctx.eval(
        'JSON.stringify(SH_toolChipLabel({kind: "tool_call", payload: '
        '{name: "workspace__write_file", arguments: {path: "src/api.ts"}}}))'
    ))
    assert write["label"] == "wrote src/api.ts"
    assert write["tone"] == "write"
    read = json.loads(ctx.eval(
        'JSON.stringify(SH_toolChipLabel({kind: "tool_call", payload: '
        '{name: "workspace__read_file", arguments: {path: "README.md"}}}))'
    ))
    assert read["tone"] == "read"


def test_raw_args_never_reach_the_chip() -> None:
    """Prohibited: raw tool JSON inline. The trace tab holds the record."""
    ctx = _ctx()
    label = ctx.eval(
        """
        SH_toolChipLabel({kind: "tool_call", payload: {
          name: "workspace__run_command",
          arguments: {command: "pytest -q", env: {SECRET: "hunter2"},
                      cwd: "/w", timeout: 900}
        }}).label
        """
    )
    assert "hunter2" not in label and "{" not in label and "timeout" not in label


def test_unknown_tools_still_get_a_verb() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_toolChipLabel({kind: "tool_call", payload: '
        '{name: "custom__do_thing", arguments: {}}}))'
    ))
    assert out["label"] == "ran do_thing"
    assert out["tone"] == "other"


# ---------------------------------------------------------------------------
# UX reconcile wave 7 (audit A items 4/6, render half): SH_toolChipLabel's
# optional resultRow argument - once the paired tool_result is in, wave
# 5's exact server metadata (grep's file_count, write/edit's diff stat)
# drives the label instead of the args-only guess.
# ---------------------------------------------------------------------------


def test_grep_result_label_becomes_searched_n_files() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toolChipLabel(
          {kind: "tool_call",
           payload: {name: "workspace__grep",
                     arguments: {pattern: "webhook", path: "src/"}}},
          {payload: {metadata: {match_count: 7, file_count: 4, truncated: false}}}
        ))
        """
    ))
    assert out["label"] == "searched 4 files"
    assert out["tone"] == "read"


def test_write_result_label_appends_diff_stat() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toolChipLabel(
          {kind: "tool_call",
           payload: {name: "workspace__write_file",
                     arguments: {path: "src/api.ts"}}},
          {payload: {output: "wrote 9 bytes to src/api.ts",
                     metadata: {additions: 2, deletions: 1}}}
        ))
        """
    ))
    assert out["label"] == "wrote src/api.ts +2 -1"
    assert out["tone"] == "write"
    # A write chip's Open-as-Tab target is unaffected by the label change.
    assert out["path"] == "src/api.ts"


def test_edit_result_label_appends_diff_stat_parsed_from_output() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toolChipLabel(
          {kind: "tool_call",
           payload: {name: "workspace__edit_file",
                     arguments: {path: "src/api.ts"}}},
          {payload: {output: "--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new\\n"}}
        ))
        """
    ))
    assert out["label"] == "edited src/api.ts +1 -1"


def test_result_label_falls_back_to_args_form_while_running() -> None:
    """No resultRow yet (still running) - identical to the pre-wave-7,
    single-arg call. Same object as test_writes_are_prominent_and_reads_are_subdued."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_toolChipLabel({kind: "tool_call", payload: '
        '{name: "workspace__write_file", arguments: {path: "src/api.ts"}}}))'
    ))
    assert out["label"] == "wrote src/api.ts"


def test_result_label_falls_back_when_result_has_no_usable_metadata() -> None:
    """A result exists but carries no metadata (pre-wave-5 record, or a
    tool with nothing to report) - args form, unchanged."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toolChipLabel(
          {kind: "tool_call",
           payload: {name: "workspace__grep",
                     arguments: {pattern: "webhook", path: "src/"}}},
          {payload: {output: "src/api.ts:88"}}
        ))
        """
    ))
    assert out["label"] == "searched src/"


def test_finished_turns_collapse_to_named_sections() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var rows = [
            {seq: 1, kind: "user_message", payload: {content: "go"}},
            {seq: 2, kind: "tool_call", payload: {name: "workspace__grep",
              arguments: {path: "src/"}}},
            {seq: 3, kind: "tool_result", payload: {tool_call_id: "tc-1"}},
            {seq: 4, kind: "tool_call", payload: {name: "workspace__read_file",
              arguments: {path: "a.ts"}}},
            {seq: 5, kind: "tool_result", payload: {tool_call_id: "tc-2"}},
            {seq: 6, kind: "assistant_message", payload: {content: "done"}},
            {seq: 7, kind: "done", payload: {}}
          ];
          return JSON.stringify(SH_collapseTurns(rows, {liveFromSeq: 99}).map(
            function (r) { return [r.kind, r.label || null, r.count || 0]; }));
        })()
        """
    ))
    assert out == [
        ["user_message", None, 0],
        ["section", "searched src/, read a.ts", 4],
        ["assistant_message", None, 0],
        ["done", None, 0],
    ]


def test_the_live_turn_stays_expanded() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var rows = [
            {seq: 10, kind: "user_message", payload: {}},
            {seq: 11, kind: "tool_call", payload: {name: "workspace__grep",
              arguments: {path: "src/"}}},
            {seq: 12, kind: "tool_result", payload: {}}
          ];
          return JSON.stringify(SH_collapseTurns(rows, {liveFromSeq: 10}).map(
            function (r) { return r.kind; }));
        })()
        """
    ))
    assert out == ["user_message", "tool_call", "tool_result"]


def test_subagent_rows_nest_under_the_delegating_call() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var rows = [
            {seq: 1, kind: "tool_call", payload: {tool_call_id: "tc-3",
              name: "system__invoke_agent", arguments: {agent_id: "builder"}}},
            {seq: 2, kind: "assistant_message", payload: {content: "sub says hi",
              delegated: true, delegate_tool_call_id: "tc-3"}},
            {seq: 3, kind: "tool_result", payload: {tool_call_id: "tc-3"}}
          ];
          var out = SH_nestSubagentRows(rows);
          return JSON.stringify([
            out.map(function (r) { return r.seq; }),
            out[0].children.map(function (r) { return r.seq; })
          ]);
        })()
        """
    ))
    assert out == [[1, 3], [2]]


def _nest(ctx, rows: list[dict]) -> list:
    """Run SH_nestSubagentRows over ``rows`` and return the tree as nested ``[seq, [children...]]``."""
    return json.loads(ctx.eval(
        "(function () {"
        "  var out = SH_nestSubagentRows(" + json.dumps(rows) + ");"
        "  function shape(r) { return [r.seq, (r.children || []).map(shape)]; }"
        "  return JSON.stringify(out.map(shape));"
        "})()"
    ))


def _call(seq, *, raw_id, scoped_id=None, **delegation):
    """A tool_call row as persistence writes it: the scoped id in ``id`` and the raw provider id in ``raw_id``."""
    payload = {"id": scoped_id or f"t1:{seq}:{raw_id}", "raw_id": raw_id, "name": "system__invoke_agent", "arguments": {}}
    payload.update(delegation)
    return {"seq": seq, "kind": "tool_call", "payload": payload}


def _delegated(seq, *, call, run=None, parent=None, kind="assistant_message"):
    payload = {"delegated": True, "delegate_tool_call_id": call}
    if run is not None:
        payload["delegate_run_id"] = run
    if parent is not None:
        payload["delegate_parent_run_id"] = parent
    return {"seq": seq, "kind": kind, "label": f"row {seq}", "payload": payload}


def test_a_real_tool_call_record_is_a_parent_by_its_raw_id() -> None:
    """Persistence writes the scoped id in ``payload.id`` and the raw provider id in ``payload.raw_id``; the delegated records
    name the RAW id. The nesting read only ``payload.tool_call_id``, which a real record does not have, so nothing ever nested."""
    ctx = _ctx()
    assert _nest(ctx, [_call(1, raw_id="call_7"), _delegated(2, call="call_7")]) == [[1, [[2, []]]]]


def test_a_record_written_before_raw_id_existed_is_a_parent_by_its_id() -> None:
    ctx = _ctx()
    old_call = {"seq": 1, "kind": "tool_call", "payload": {"id": "call_7", "name": "system__invoke_agent"}}
    assert _nest(ctx, [old_call, _delegated(2, call="call_7")]) == [[1, [[2, []]]]]


def test_nested_runs_that_reuse_one_raw_call_id_nest_by_run() -> None:
    """The child's own call reuses the parent's raw id ``call_0`` (providers that synthesise ids restart the numbering every
    stream). By raw id alone the child's LATER records nested under its own call, and the grandchild under whichever call came
    last. By the run that made the delegating call each lands under the right one, and the child's own call nests too."""
    ctx = _ctx()
    rows = [
        _call(1, raw_id="call_0"),                                                          # the parent's delegating call
        _delegated(2, call="call_0", run="R1"),                                             # the child, before it delegates
        _call(3, raw_id="call_0", delegated=True, delegate_tool_call_id="call_0", delegate_run_id="R1"),  # its own call
        _delegated(4, call="call_0", run="R2", parent="R1"),                                # the grandchild
        _delegated(5, call="call_0", run="R1"),                                             # the child, after
    ]
    assert _nest(ctx, rows) == [[1, [[2, []], [3, [[4, []]]], [5, []]]]]


def test_a_record_with_a_run_id_whose_delegating_call_is_missing_stays_at_the_top() -> None:
    """Exact by run: a call with the same raw id made by ANOTHER run is not the one that delegated."""
    ctx = _ctx()
    rows = [_call(1, raw_id="call_0"), _delegated(2, call="call_0", run="R9", parent="R-not-seen")]
    assert _nest(ctx, rows) == [[1, []], [2, []]]


def test_records_without_run_ids_still_nest_by_raw_id() -> None:
    """An older log: the delegated rows carry no run id and the call is found by its raw id."""
    ctx = _ctx()
    assert _nest(ctx, [_call(1, raw_id="call_7"), _delegated(2, call="call_7"), _delegated(3, call="call_7")]) == [
        [1, [[2, []], [3, []]]],
    ]


def _flatten(shape) -> list[int]:
    return [seq for seq, kids in shape for seq in [seq, *_flatten(kids)]]


def test_a_delegated_run_written_by_the_real_writers_nests_in_the_console_tree() -> None:
    """The records come from ``translate_stream_event`` and the real ``DelegationRecorder`` (tests/ui_e2e/_delegation_seed.py, which the
    Playwright journey seeds the same way), not from a dict written to fit the code: the first test of this nesting used a hand-built
    ``tool_call_id`` shape that a real record does not have, which is how it never worked."""
    from tests.ui_e2e import _delegation_seed as seed

    seeded = seed.build()
    ctx = _ctx()
    tree = json.loads(ctx.eval(
        "(function () {"
        "  var rows = SH_nestSubagentRows(SA_toTranscript(" + json.dumps(seeded.records) + ", null));"
        "  function shape(r) { return [r.seq, (r.children || []).map(shape)]; }"
        "  return JSON.stringify(rows.map(shape));"
        "})()"
    ))
    delegated = {r["seq"] for r in seeded.records if r["payload"].get("delegated")}
    grandchild = {r["seq"] for r in seeded.records if r["payload"].get("delegate_run_id") == seed.RUN_GRANDCHILD}
    by_seq = {seq: kids for seq, kids in tree}
    assert not delegated & set(by_seq), "a delegated row is at the top level"
    parent_kids = by_seq[seeded.parent_call_seq]
    assert delegated <= set(_flatten(parent_kids)), "every delegated row nests under the parent's invoke_agent call"
    child_call = next(kids for seq, kids in parent_kids if seq == seeded.child_call_seq)
    assert grandchild and grandchild <= set(_flatten(child_call)), "the grandchild's rows nest under the CHILD's own call"
    assert not grandchild & {seq for seq, _ in parent_kids}, "and not directly under the parent's call"


def test_the_trace_ordinal_counts_the_sessions_turns_and_not_a_subagents() -> None:
    """Ticket 01a11232: the console numbers turns the way the timeline endpoint does (``terminals.closes_turn``), because the
    number it asks the trace for is that endpoint's window ordinal. A delegated run's ``done`` is the SUBAGENT's turn end: counted,
    the trace of every turn after a delegation was asked for under the wrong number."""
    from tests.ui_e2e import _delegation_seed as seed

    seeded = seed.build()
    last = seeded.records[-1]["seq"]
    records = seeded.records + [
        {"seq": last + 1, "kind": "user_input", "payload": {"text": "and again"}, "created_at": "2026-10-06T12:01:00Z"},
        {"seq": last + 2, "kind": "done", "payload": {"stop_reason": "stop"}, "created_at": "2026-10-06T12:01:01Z"},
    ]
    ctx = _ctx()
    ordinals = json.loads(ctx.eval(
        "JSON.stringify(SH_turnOfSeq(SA_toTranscript(" + json.dumps(records) + ", null), " + _windows_js(json.dumps(records)) + "))"
    ))
    first_turn = {str(r["seq"]) for r in seeded.records} & set(ordinals)
    assert first_turn and {ordinals[seq] for seq in first_turn} == {0}, "the delegated dones must not start a new turn"
    assert ordinals[str(last + 1)] == 1 and ordinals[str(last + 2)] == 1


def test_the_trace_ordinal_still_splits_turns_at_the_sessions_own_terminals() -> None:
    ctx = _ctx()
    ordinals = json.loads(ctx.eval(
        """(function () {
          var records = [
            {seq: 1, kind: "user_input", payload: {}},
            {seq: 2, kind: "done", payload: {stop_reason: "stop"}},
            {seq: 3, kind: "user_input", payload: {}},
            {seq: 4, kind: "error", payload: {}},
            {seq: 5, kind: "user_input", payload: {}},
            {seq: 6, kind: "cancelled", payload: {}},
            {seq: 7, kind: "user_input", payload: {}},
            {seq: 8, kind: "done", payload: {stop_reason: "tool_use"}},
            {seq: 9, kind: "done", payload: {stop_reason: "stop"}},
            {seq: 10, kind: "error", payload: {delegated: true}},
            {seq: 11, kind: "user_input", payload: {}},
            {seq: 12, kind: "done", payload: {stop_reason: "tool_turn_cap"}},
            {seq: 13, kind: "user_input", payload: {}}
          ];
          return JSON.stringify(SH_turnOfSeq(records, SH_windowsOfSeq(records)));
        })()"""
    ))
    assert [ordinals[str(i)] for i in range(1, 11)] == [0, 0, 1, 1, 2, 2, 3, 3, 3, 4]
    # a done that says the tool-turn cap stopped the run is not a tool round: it ends the turn (#437)
    assert [ordinals[str(i)] for i in (11, 12, 13)] == [4, 4, 5]


def test_the_console_numbers_turns_like_the_server_for_every_fatal_variant_of_an_error() -> None:
    """Ticket 01a11bf6: the server no longer counts an ERROR with an explicit ``fatal: false`` as a turn end (a recoverable stream error
    is a notice), and ``SH_closesTurn`` is the console's mirror of ``closes_turn``: the trace is asked for under the ordinal counted
    here, so the two must agree record for record. Compared against the REAL server windows, not a copy of the rule."""
    payloads = [
        {"fatal": False},
        {"fatal": True},
        {"fatal": None},
        {},
        {"fatal": False, "delegated": True},
        {"fatal": True, "delegated": True},
    ]
    records: list[dict] = []
    for payload in payloads:
        records.append({"seq": len(records) + 1, "kind": "user_input", "payload": {}})
        records.append({"seq": len(records) + 1, "kind": "error", "payload": payload})
    records.append({"seq": len(records) + 1, "kind": "user_input", "payload": {}})
    records.append({"seq": len(records) + 1, "kind": "done", "payload": {"stop_reason": "stop"}})

    expected = {str(seq): window for seq, window in _server_windows(records).items()}

    ordinals = json.loads(_ctx().eval(
        "JSON.stringify(SH_turnOfSeq(" + json.dumps(records) + ", SH_windowsOfSeq(" + json.dumps(records) + ")))"
    ))

    assert ordinals == expected, f"the console and the server number the turns differently: {ordinals} vs {expected}"


def test_the_session_doc_takes_its_trace_ordinal_from_the_shared_function() -> None:
    doc = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
    assert "SH_turnOfSeq(flat, SH_windowsOfSeq(records))" in doc, "the ordinal must come from ALL the raw records, as the server counts them"
    assert 'flat[ti].kind === "done" || flat[ti].kind === "cancelled"' not in doc, "the inline copy of the rule is back"


def test_flat_interleaving_is_not_produced_when_attribution_is_absent() -> None:
    """A record with no delegate key is an ordinary row, not a lost child."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_nestSubagentRows(['
        '{seq: 1, kind: "assistant_message", payload: {content: "hi"}}'
        ']).map(function (r) { return [r.seq, (r.children || []).length]; }))'
    ))
    assert out == [[1, 0]]


def test_a_write_chip_carries_the_path_it_opens() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_toolChipLabel({kind: "tool_call", '
        'label: "workspace__write_file", '
        'payload: {name: "workspace__write_file", '
        'arguments: {path: "src/api.ts"}}}))'
    ))
    assert out["tone"] == "write"
    assert out["path"] == "src/api.ts"


def test_a_non_fatal_error_mid_turn_is_numbered_the_way_the_servers_timeline_numbers_it() -> None:
    """The adapter draws a non-fatal stream Error as a retry notice instead of an error card (ticket 01a11bcc). The server does not count an ERROR with an
    explicit ``fatal: false`` as a turn end (ticket 01a11bf6), and the console asks the trace for the server's window ordinal, so the notice row must not count
    in ``SH_turnOfSeq`` either. Compared against the real server windows over the same records."""
    records = [
        {"seq": 1, "kind": "user_input", "payload": {"text": "go"}, "created_at": "t1"},
        {"seq": 2, "kind": "error", "payload": {"message": "hiccup", "code": "server_error", "fatal": False}, "created_at": "t2"},
        {"seq": 3, "kind": "assistant_token", "payload": {"text": "carried on"}, "created_at": "t3"},
        {"seq": 4, "kind": "done", "payload": {"stop_reason": "stop"}, "created_at": "t4"},
        {"seq": 5, "kind": "user_input", "payload": {"text": "again"}, "created_at": "t5"},
        {"seq": 6, "kind": "error", "payload": {"message": "x", "code": "y", "fatal": False, "delegated": True, "delegate_run_id": "r1"}, "created_at": "t6"},
        {"seq": 7, "kind": "done", "payload": {"stop_reason": "stop"}, "created_at": "t7"},
    ]
    expected = {str(seq): window for seq, window in _server_windows(records).items()}
    ctx = _ctx()
    got = json.loads(ctx.eval(
        "JSON.stringify(SH_turnOfSeq(SA_toTranscript(" + json.dumps(records) + ", null), " + _windows_js(json.dumps(records)) + "))"
    ))
    assert {seq: got[seq] for seq in expected} == expected


def _both_ordinals(records: list[dict]) -> tuple[dict[int, int], dict[int, int]]:
    """(the server's window per seq, the console's) for the same records. The server's is the REAL ``turn_windows``; the console numbers what it really
    draws: ``SH_nestSubagentRows(SA_toTranscript(records))``, in which the failure fold has removed the copies and the marker, from the window map of the
    raw records (``SH_windowsOfSeq``)."""
    server = _server_windows(records)
    ctx = _ctx()
    got = json.loads(ctx.eval(
        "JSON.stringify(SH_turnOfSeq(SH_nestSubagentRows(SA_toTranscript(" + json.dumps(records) + ", null)), "
        + _windows_js(json.dumps(records)) + "))"
    ))
    return server, {int(seq): n for seq, n in got.items()}


def _r(seq: int, kind: str, node_id: str | None = None, **payload) -> dict:
    rec = {"seq": seq, "kind": kind, "payload": payload, "created_at": f"t{seq}"}
    if node_id:
        rec["node_id"] = node_id
    return rec


def _following_turn(first_seq: int) -> list[dict]:
    return [
        _r(first_seq, "user_input", text="again"),
        _r(first_seq + 1, "assistant_token", text="fine this time"),
        _r(first_seq + 2, "done", stop_reason="stop"),
    ]


def test_a_failed_turn_with_its_release_marker_leaves_the_next_turn_numbered_as_the_server_numbers_it() -> None:
    """The release path writes a bare terminal marker after a failure, and the server counts it as a turn end of its own; the failure fold
    removes it from what the console draws, so the console undercounted every turn after a failed one and asked the trace for the wrong window."""
    records = [
        _r(1, "user_input", text="go"),
        _r(2, "error", message="boom", code="server_error"),
        _r(3, "error", reason="unknown", terminal=True),
        *_following_turn(4),
    ]
    server, console = _both_ordinals(records)
    assert {seq: console[seq] for seq in console} == {seq: server[seq] for seq in console}, (server, console)


def test_a_stream_error_its_dispatch_copy_and_the_marker_all_end_the_turn_on_the_server_and_in_the_console() -> None:
    records = [
        _r(1, "user_input", text="go"),
        _r(2, "error", message="boom", code="server_error", fatal=True),
        _r(3, "error", message="boom", code="/errors/internal"),
        _r(4, "error", reason="unknown", terminal=True),
        *_following_turn(5),
    ]
    server, console = _both_ordinals(records)
    assert {seq: console[seq] for seq in console} == {seq: server[seq] for seq in console}, (server, console)


def test_a_subagents_failure_does_not_shift_the_sessions_turn_numbers() -> None:
    sub = {"delegated": True, "delegate_tool_call_id": "call-1", "delegate_run_id": "run-1"}
    records = [
        _r(1, "user_input", text="go"),
        _r(2, "error", message="boom", code="server_error", fatal=True, **sub),
        _r(3, "error", message="boom", code="server_error", fatal=True, **sub),
        _r(4, "assistant_token", text="the parent carries on"),
        _r(5, "done", stop_reason="stop"),
        *_following_turn(6),
    ]
    server, console = _both_ordinals(records)
    assert {seq: console[seq] for seq in console} == {seq: server[seq] for seq in console}, (server, console)


def test_the_real_producers_failure_sequence_is_numbered_as_the_server_numbers_it() -> None:
    """What the agent loop and dispatch really write for a non-fatal stream Error: the loop holds it, raises when the stream ends, and dispatch writes
    its own ERROR with the same words and then the release marker: [done(error), error{M, fatal: false}, error{M}, marker]. The console draws ONE red card
    (the failure absorbs the notice) and must still number the turn after it as the server does: the server files the done(error), the notice, the
    failure and the marker in ONE window (the done ends it, the rest are its copies), so the turn after it is the second."""
    records = [
        _r(1, "user_input", text="go"),
        _r(2, "done", stop_reason="error"),
        _r(3, "error", message="boom", code="server_error", fatal=False),
        _r(4, "error", message="boom", code="/errors/internal"),
        _r(5, "error", reason="unknown", terminal=True),
        *_following_turn(6),
    ]
    server, console = _both_ordinals(records)
    assert 3 not in console, "the notice was absorbed by the failure with the same words"
    assert {seq: console[seq] for seq in console} == {seq: server[seq] for seq in console}, (server, console)
    assert console[6] == 1 and console[8] == 1, "and the following turn is the second window, as on the server: the failed turn is one"


def _same_ordinals(records: list[dict]) -> None:
    server, console = _both_ordinals(records)
    assert {seq: console[seq] for seq in console} == {seq: server[seq] for seq in console}, (server, console)


def test_text_streamed_between_a_failure_and_its_copy_keeps_its_own_ordinal() -> None:
    """The count of folded terminals used to be added at the SURVIVING row's position, so every drawn row between the cause and a copy folded into it was
    numbered too high (server: the text is in the failure's window; the copy ends a LATER window)."""
    _same_ordinals([
        _r(1, "user_input", text="go"),
        _r(2, "error", message="boom", code="server_error", fatal=True),
        _r(3, "assistant_token", text="a sentence between"),
        _r(4, "error", message="boom", code="/errors/internal"),
        _r(5, "error", reason="unknown", terminal=True),
        *_following_turn(6),
    ])


def test_the_graph_superstep_failure_is_one_window_with_its_siblings_rows() -> None:
    """A failed node's error is written live, a sibling streams on, the graph's copy of the error comes after the superstep, and dispatch's failure exit ends the turn. Every one of them but
    the last is a NODE's record, so it is inside the graph turn's window (01a11f35): the sibling's rows are in the failed node's window, and the next turn is the next ordinal."""
    records = [
        _r(1, "user_input", text="go"),
        _r(2, "llm_call"),
        _r(3, "error", node_id="A", message="boom", code="server_error", fatal=True),
        _r(4, "graph_transition", node_id="A", phase="exit", status="failed"),
        _r(5, "assistant_token", node_id="B", text="b answers"),
        _r(6, "llm_call", node_id="B"),
        _r(7, "done", node_id="B", stop_reason="stop"),
        _r(8, "graph_transition", node_id="B", phase="exit", status="completed"),
        _r(9, "error", node_id="A", message="boom", code="server_error"),
        _r(10, "error", message="boom", code="/errors/internal", title="Internal error", status=500),
        *_following_turn(11),
    ]
    _same_ordinals(records)
    _, console = _both_ordinals(records)
    assert {console[seq] for seq in console if seq <= 10} == {0}, console
    assert {console[seq] for seq in console if seq >= 11} == {1}, console


def test_a_marker_written_before_its_cause_is_numbered_as_the_server_numbers_it() -> None:
    _same_ordinals([
        _r(1, "user_input", text="go"),
        _r(2, "error", reason="unknown", terminal=True),
        _r(3, "assistant_token", text="x"),
        _r(4, "error", message="boom", code="server_error"),
        *_following_turn(5),
    ])


def test_two_failed_turns_in_a_row_are_numbered_as_the_server_numbers_them() -> None:
    _same_ordinals([
        _r(1, "user_input", text="go"),
        _r(2, "error", message="boom", code="server_error", fatal=True),
        _r(3, "error", message="boom", code="/errors/internal"),
        _r(4, "error", reason="unknown", terminal=True),
        _r(5, "user_input", text="again"),
        _r(6, "error", message="boom", code="server_error", fatal=True),
        _r(7, "error", message="boom", code="/errors/internal"),
        _r(8, "error", reason="unknown", terminal=True),
        *_following_turn(9),
    ])


def test_the_console_scanner_gives_the_servers_verdict_on_every_record_of_every_shape_the_writers_produce() -> None:
    """``SH_newWindowScanner`` is the mirror of ``terminals.TurnWindowScanner`` (ticket 01a11ca5): the same verdict for every record, so the window each
    record is filed in (``SH_windowsOfSeq``) is the one the timeline endpoint files it in (``turn_windows``) and the trace is asked for the right ordinal.
    The shapes are the ones tests/session/test_failed_turn_is_one_window.py runs the server readers over; compared against the REAL scanner."""
    from primer.session.terminals import TurnWindowScanner
    from primer.session.timeline import turn_windows
    from tests.session.test_failed_turn_is_one_window import SHAPES

    ctx = _ctx()
    for name, lines, _ends, _window_seqs in SHAPES:
        records = [json.loads(line) for line in lines]
        scanner = TurnWindowScanner()
        want = [scanner.feed(rec) for rec in records]
        got = json.loads(ctx.eval(
            "(function () { var s = SH_newWindowScanner(); return JSON.stringify(" + json.dumps(records) + ".map(function (r) { return s.feed(r); })); })()"
        ))
        assert got == want, (name, got, want)

        windows = turn_windows(lines)
        filed = json.loads(ctx.eval("JSON.stringify(SH_windowsOfSeq(" + json.dumps(records) + "))"))
        console = {int(seq): n for seq, n in filed["of"].items()}
        server = {rec["seq"]: w["turn_no"] for w in windows for rec in w["records"]}       # the visible ones: a rewound record has no row to number
        assert {seq: console[seq] for seq in server} == server, name
        assert filed["open"] == sum(1 for w in windows if w["terminal_seq"] is not None), name


def test_a_row_the_records_do_not_hold_is_numbered_by_where_it_sits() -> None:
    """An optimistic or live row has no record: before the first record it belongs to the first window, after the last to the one still open."""
    records = [
        {"seq": 5, "kind": "user_input", "payload": {}},
        {"seq": 6, "kind": "done", "payload": {"stop_reason": "stop"}},
        {"seq": 7, "kind": "user_input", "payload": {}},
    ]
    rows = [{"seq": -1}, {"seq": 5}, {"seq": 7}, {"seq": 99}]
    got = json.loads(_ctx().eval(
        "JSON.stringify(SH_turnOfSeq(" + json.dumps(rows) + ", SH_windowsOfSeq(" + json.dumps(records) + ")))"
    ))
    assert got == {"-1": 0, "5": 0, "7": 1, "99": 1}


def test_the_console_judges_a_record_of_the_wrong_type_like_the_server():
    """A payload that is not an object reads as an empty one and a message that is not a string as none, on both sides, and neither side crashes."""
    from primer.session.terminals import TurnWindowScanner
    from tests.session.test_failed_turn_is_one_window import WEIRD_RECORDS

    scanner = TurnWindowScanner()
    want = [scanner.feed(rec) for rec in WEIRD_RECORDS]
    got = json.loads(_ctx().eval(
        "(function () { var s = SH_newWindowScanner(); return JSON.stringify(" + json.dumps(WEIRD_RECORDS) + ".map(function (r) { return s.feed(r); })); })()"
    ))

    assert got == want, (got, want)


def test_the_trace_ordinal_of_a_turn_after_a_rewind_is_the_servers():
    """The server counts every record (turn_windows, unfolded), so a rewound turn still takes its ordinal. Numbered from the VISIBLE records the
    console asked the trace for n=1 and n=2 where the server files the same turns in 2 and 3."""
    records = [
        _r(1, "user_input", text="a"), _r(2, "done", stop_reason="stop"),
        _r(3, "user_input", text="b"), _r(4, "done", stop_reason="stop"),
        _r(5, "rewind_marker", to_seq=2),
        _r(6, "user_input", text="c"), _r(7, "done", stop_reason="stop"),
        _r(8, "user_input", text="d"), _r(9, "done", stop_reason="stop"),
    ]
    server, console = _both_ordinals(records)

    assert {seq: console[seq] for seq in console if seq in server} == {seq: server[seq] for seq in console if seq in server}, (server, console)
    assert console[6] == 2 and console[7] == 2 and console[8] == 3 and console[9] == 3, console

