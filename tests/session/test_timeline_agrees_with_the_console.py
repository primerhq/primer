"""The server's timeline and the console's transcript fold the same records into the same tree (tickets 01a11cca, 01a11cd4, the lead's review of #660).

Both readers nest a delegated record under the call that delegated to it and pair a result with the call that made it, and each was written on its own. They agree on records written by the
current producer; they disagreed on records written BEFORE run ids existed (``delegated`` and ``delegate_tool_call_id`` only):

* a delegated call there carries the same raw id as the call that delegated to it (providers that synthesise ids reuse ``call_0``). The timeline registered the call in its raw-id map BEFORE it
  looked up where to attach it, so it attached to ITSELF and vanished from the tree with everything under it; the console looks the parent up first;
* the console pairs those records under the scope ``call:<delegating raw id>`` (``SH_callScope``), the timeline under the parent turn's scope, so a delegated call and its parent's, which share a
  scoped id, took each other's results.

Each case folds the records with BOTH readers and compares, per call: its parent call, its result's output, and its status.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.session.timeline import build_turn_timeline
from tests.ui._mini_react import mini_react_context, transpile
from tests.ui_e2e import _delegation_seed as seed

ROOT = Path(__file__).resolve().parents[2]
RUN_FIELDS = ("delegate_run_id", "delegate_parent_run_id", "delegate_depth", "delegate_node_id")


def _without(records: list[dict], names: tuple[str, ...]) -> list[dict]:
    out = []
    for rec in records:
        rec = json.loads(json.dumps(rec))
        for name in names:
            rec["payload"].pop(name, None)
        out.append(rec)
    return out


def _py_view(records: list[dict]) -> dict[int, list]:
    """call seq -> [the seq of the call it is nested under or None, its result's output, its status], from the timeline."""
    tl = build_turn_timeline(message_lines=[json.dumps(r) for r in records], turn_log_lines=[], turn_no=0)
    out: dict[int, list] = {}

    def walk(node: dict, parent_call: int | None, seen: frozenset) -> None:
        for child in node.get("children", []):
            if id(child) in seen:
                continue
            if child["kind"] == "tool_call":
                result = child.get("result") or {}
                out[child["seq"]] = [parent_call, result.get("output"), child.get("status")]
                walk(child, child["seq"], seen | {id(child)})
            else:
                walk(child, parent_call, seen | {id(child)})

    walk(tl, None, frozenset())
    return out


def _js_view(records: list[dict]) -> dict[int, list]:
    ui = ROOT / "ui"
    prelude = "\n".join([
        (ui / "foundation" / "shell-status.js").read_text(encoding="utf-8"),
        transpile(ui / "components" / "session-adapter.jsx"),
        (ui / "foundation" / "shell-turns.js").read_text(encoding="utf-8"),
    ])
    ctx = mini_react_context("", prelude)
    try:
        raw = ctx.eval(
            "(function () { var n = SH_nestWithResults(SA_toTranscript(" + json.dumps(records) + ", null)); var out = {};"
            " function walk(rows, pc) { rows.forEach(function (r) { if (r.kind === 'tool_call') { var res = SH_resultForCall(n.resultsByCallId, r);"
            " out[r.seq] = [pc, res ? res.payload.output : null, res ? (res.payload.error ? 'error' : 'ok') : null]; walk(r.children || [], r.seq); }"
            " else walk(r.children || [], pc); }); }"
            " walk(n.flat, null); return JSON.stringify(out); })()"
        )
        return {int(k): v for k, v in json.loads(raw).items()}
    finally:
        ctx.close()


def _normal(view: dict[int, list]) -> dict[int, list]:
    return {seq: [pc, out if out is None or isinstance(out, str) else json.dumps(out), status] for seq, [pc, out, status] in view.items()}


SCENARIOS = {
    "the current producer's delegation": lambda: seed.build().records,
    "the current producer's failures": lambda: seed.build(failures=True).records,
    "a nested notice": lambda: seed.build_nested_notice(False).records,
    "the fan-out of two nodes": lambda: seed.build_fanout().records,
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_readers_agree_on_records_the_current_producer_writes(name: str) -> None:
    """The control: the shapes that already agreed keep agreeing."""
    records = SCENARIOS[name]()
    assert _normal(_py_view(records)) == _normal(_js_view(records))


@pytest.mark.parametrize("name", ["the current producer's delegation", "the current producer's failures", "a nested notice"])
def test_the_readers_agree_on_records_from_before_run_ids(name: str) -> None:
    records = _without(SCENARIOS[name](), RUN_FIELDS)
    py, js = _normal(_py_view(records)), _normal(_js_view(records))
    diffs = {seq: (py.get(seq), js.get(seq)) for seq in sorted(set(py) | set(js)) if py.get(seq) != js.get(seq)}
    assert diffs == {}, f"(timeline, console) per call seq: {diffs}"


def test_a_delegated_call_from_before_run_ids_keeps_its_place_and_its_subtree() -> None:
    """The timeline attached it to ITSELF (its raw id equals its delegating call's, and it had just registered itself under that id)."""
    seeded = seed.build()
    records = _without(seeded.records, RUN_FIELDS)
    view = _py_view(records)
    assert view[seeded.child_call_seq][0] == seeded.parent_call_seq, view
    tl = build_turn_timeline(message_lines=[json.dumps(r) for r in records], turn_log_lines=[], turn_no=0)

    def find(node: dict, seq: int):
        for child in node.get("children", []):
            if child.get("seq") == seq:
                return child
            hit = find(child, seq)
            if hit is not None:
                return hit
        return None

    child_call = find(tl, seeded.child_call_seq)
    assert child_call is not None and child_call["name"] == "system__invoke_agent", "the delegated call vanished from the tree"
