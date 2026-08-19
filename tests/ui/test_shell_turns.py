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


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval((ROOT / "ui" / "foundation" / "shell-status.js").read_text(encoding="utf-8"))
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
