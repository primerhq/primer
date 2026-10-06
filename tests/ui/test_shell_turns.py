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
