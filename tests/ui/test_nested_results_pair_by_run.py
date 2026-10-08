"""A nested call is paired with ITS OWN run's result, so a grandchild's failed call is not read off its parent's result (review of #575, round 2).

``DelegationRecorder`` DOES write a delegated run's tool results (``run_agent_turn`` yields the ``_ExecutorToolResult`` of a subagent's call, ``run_subagent`` feeds it
to the recorder, ``translate_stream_event`` writes it as a ``tool_result`` stamped ``delegated`` with the run's ids), and the recorder's per-run coalescing state numbers the
scoped call ids again for every run: a child's call and its parent's are both ``x:tool:1:1``. The console indexed only the TOP-LEVEL results, by that scoped id alone, so
a nested call block showed its PARENT's result, and the failed-call rule ("a notice whose delegating call FAILED is the failure") read the parent's OK result for a
grandchild's call and said "carried on", or, when the ids did not collide, found no result and said "the turn is continuing" under a finished parent.

These run the real pipeline step (``SH_nestWithResults`` on ``SA_toTranscript``'s output) and the real ``NV_subagentRows`` on records written by the real recorder
(``tests/ui_e2e/_delegation_seed.py``), in the loop's real order: for a grandchild that ends the way the OpenResponses stream does, the Done first, the held non-fatal
Error last, then the helper's call to it is answered with an ERROR result that quotes the stream error.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile
from tests.ui_e2e import _delegation_seed as seed

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = DOC.index("function " + name + "(")
    return DOC[start:DOC.index("\n}\n", start) + len("\n}\n")]


@functools.lru_cache(maxsize=1)
def _compiled() -> tuple[str, str]:
    """The snippet and the prelude, transpiled once for the module (the Babel bundler takes seconds to start)."""
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        snippet = bundler._transform(
            "\n".join(_function(n) for n in ("NV_failureWords", "NV_errorView", "NV_noticeView", "NV_subagentRows", "NV_toolCallWithRows")), "snippet.jsx")
    finally:
        bundler._ctx.close()
    prelude = "\n".join([
        (ui / "foundation" / "shell-status.js").read_text(encoding="utf-8"),
        transpile(ui / "components" / "session-adapter.jsx"),
        (ui / "foundation" / "shell-turns.js").read_text(encoding="utf-8"),
        # What the call's own block says about its result, so a test can read WHICH result a call was paired with.
        "function NV_toolCallElement(row, result) { return React.createElement('div', {'data-testid': 'tool:' + row.seq}, "
        "'[call ' + row.seq + ' result=' + (result ? (result.seq + ':error=' + result.payload.error) : 'none') + ']'); }",
    ])
    return snippet, prelude


def _context():
    snippet, prelude = _compiled()
    return mini_react_context(snippet, prelude)


@pytest.fixture
def render():
    made = []

    def go(records: list[dict], call_seq: int):
        ctx = _context()
        made.append(ctx)
        ctx.eval(
            "var NESTED = SH_nestWithResults(SA_toTranscript(" + json.dumps(records) + ", null));"
            "function resultFor(row) { return SH_resultForCall(NESTED.resultsByCallId, row); }"
            "var CALL = NESTED.flat.filter(function (r) { return r.seq === " + str(call_seq) + "; })[0];"
            "function Probe() { return NV_toolCallWithRows(CALL, resultFor, false); } MR.mount(Probe, {});"
        )
        return ctx

    try:
        yield go
    finally:
        for ctx in made:
            ctx.close()


def _subtree(ctx, testid: str) -> list[dict]:
    nodes = json.loads(ctx.eval(f'JSON.stringify(MR.subtree("{testid}"))'))
    assert nodes, f"nothing is drawn inside {testid}: the walk below would pass on an empty box"
    return nodes


def _red_cards(ctx, testid: str) -> int:
    return sum(1 for el in _subtree(ctx, testid) if "nv-turn-error" in el["className"].split())


def _result_seq(records: list[dict], *, run: str | None, call_id: str) -> int:
    return next(r["seq"] for r in records if r["kind"] == "tool_result" and r["payload"].get("call_id") == call_id and r["payload"].get("delegate_run_id") == run)


def _call_text(ctx, seq: int) -> str:
    return ctx.eval(f"MR.texts().filter(function (t) {{ return t.indexOf('[call {seq} ') === 0; }})[0]")


@pytest.mark.parametrize("failures", [False, True])
def test_a_nested_call_shows_its_own_result_and_the_parent_call_its_own(render, failures: bool) -> None:
    seeded = seed.build(failures=failures)
    parent_call = next(r for r in seeded.records if r["seq"] == seeded.parent_call_seq)
    child_call = next(r for r in seeded.records if r["seq"] == seeded.child_call_seq)
    assert parent_call["payload"]["id"] == child_call["payload"]["id"], "the seed is only meaningful while the two calls share a scoped id"
    parent_result = _result_seq(seeded.records, run=None, call_id=parent_call["payload"]["id"])
    child_result = _result_seq(seeded.records, run=seed.RUN_CHILD, call_id=child_call["payload"]["id"])

    ctx = render(seeded.records, seeded.parent_call_seq)

    assert _call_text(ctx, seeded.parent_call_seq) == f"[call {seeded.parent_call_seq} result={parent_result}:error=false]"
    assert _call_text(ctx, seeded.child_call_seq) == f"[call {seeded.child_call_seq} result={child_result}:error={'true' if failures else 'false'}]"


@pytest.mark.parametrize("helper_calls_something_first", [False, True])
def test_a_grandchilds_notice_is_the_one_red_card_of_the_call_that_failed(render, helper_calls_something_first: bool) -> None:
    """The helper's call to the grandchild is answered with an ERROR result that quotes the notice; the notice is that call's failure, in its own words."""
    seeded = seed.build_nested_notice(helper_calls_something_first)
    notice = seeded.delegated_notice_seq
    ctx = render(seeded.records, seeded.parent_call_seq)

    assert ctx.eval(f'MR.find("nv-subagent-failure:{notice}") !== null'), "the notice is drawn as the failure"
    assert ctx.eval(f'MR.find("nv-subagent-notice:{notice}") === null'), "and not as the quiet line"
    assert _red_cards(ctx, f"nv-subagent-failure:{notice}") == 1
    text = ctx.eval("MR.texts().join(' | ')")
    assert seed.GRAND_NOTICE in text
    assert "carried on" not in text and "continuing" not in text
    # the helper's failing call was paired with the result of ITS OWN run, an error
    helper_result = _result_seq(seeded.records, run=seed.RUN_CHILD, call_id=next(
        r["payload"]["id"] for r in seeded.records if r["seq"] == seeded.child_call_seq))
    assert _call_text(ctx, seeded.child_call_seq) == f"[call {seeded.child_call_seq} result={helper_result}:error=true]"


def test_a_result_of_another_run_with_the_same_scoped_id_is_not_this_calls(render) -> None:
    """Pairing is by (run, scoped id): the parent's own result must not answer the helper's call, and the helper's must not answer the parent's."""
    seeded = seed.build_nested_notice()
    ctx = render(seeded.records, seeded.parent_call_seq)
    calls = {r["seq"]: r["payload"]["id"] for r in seeded.records if r["kind"] == "tool_call"}
    assert calls[seeded.parent_call_seq] == calls[seeded.child_call_seq]
    assert ctx.eval("Object.keys(NESTED.resultsByCallId).length") == 2, "one result per (run, call id): the parent's and the helper's"


def _before_run_ids(records: list[dict]) -> list[dict]:
    """The log a deployment wrote before runs had ids: a delegated record carries ``delegated`` and the delegating call's raw id, and no run id."""
    out = copy.deepcopy(records)
    for r in out:
        r["payload"].pop("delegate_run_id", None)
        r["payload"].pop("delegate_parent_run_id", None)
    return out


@pytest.mark.parametrize("failures", [False, True])
def test_records_from_before_run_ids_still_pair_each_call_with_its_own_result(render, failures: bool) -> None:
    """With no run id the scope of a delegated record is the raw id of the call that delegated (``call:<id>``); the parent's own records have none.

    The parent's call and the helper's call still share a scoped id (``x:tool:1:1``), so a reader with no scope for them would give both the result indexed last.
    """
    seeded = seed.build(failures=failures)
    records = _before_run_ids(seeded.records)
    assert not any("delegate_run_id" in r["payload"] for r in records)
    assert any(r["payload"].get("delegated") and r["payload"].get("delegate_tool_call_id") for r in records), "the delegated records stay marked"
    parent_call = next(r for r in records if r["seq"] == seeded.parent_call_seq)
    child_call = next(r for r in records if r["seq"] == seeded.child_call_seq)
    assert parent_call["payload"]["id"] == child_call["payload"]["id"]
    parent_result = next(r["seq"] for r in records if r["kind"] == "tool_result" and not r["payload"].get("delegated") and r["payload"]["call_id"] == parent_call["payload"]["id"])
    child_result = next(r["seq"] for r in records if r["kind"] == "tool_result" and r["payload"].get("delegated") and r["payload"]["call_id"] == child_call["payload"]["id"])

    ctx = render(records, seeded.parent_call_seq)

    assert _call_text(ctx, seeded.parent_call_seq) == f"[call {seeded.parent_call_seq} result={parent_result}:error=false]"
    assert _call_text(ctx, seeded.child_call_seq) == f"[call {seeded.child_call_seq} result={child_result}:error={'true' if failures else 'false'}]"


def test_the_session_doc_pairs_through_the_shared_pipeline_step_and_lookup() -> None:
    """The V8 tests above run ``SH_nestWithResults`` / ``SH_resultForCall`` themselves; this pins that the session tab is wired to them.

    A component that went back to indexing the top-level results by scoped id alone would pass every test above and fail the reader of a grandchild's notice.
    """
    body = _function("NV_SessionDoc")
    assert re.search(r"SH_nestWithResults\(\s*window\.SA_toTranscript\(", body), "the pipeline's nesting is SH_nestWithResults over the adapter's transcript"
    assert re.search(r"function resultFor\(row\)\s*\{\s*return SH_resultForCall\(resultsByCallId, row\);\s*\}", body), "a call's result is SH_resultForCall's"
    assert "SH_nestSubagentRows(" not in body, "the rows are nested by the pipeline step, after the results are indexed"
    assert not re.search(r"resultsByCallId\[[^\]]+\]\s*=", body), "no second hand-built index of results"
