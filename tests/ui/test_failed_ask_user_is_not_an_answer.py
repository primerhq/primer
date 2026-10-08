"""A failed ask_user call is a failed tool call, not a green "Resolved" answer card (console review 2026-10-08, C-013).

``NV_toolCallElement`` drew the answered-ask card for ANY ``ask_user`` call that had a result. When the model called it with a wrong argument name the result
was the tool's validation error (``error: true``, output ``{"type": "validation-error", "message": "argument validation failed: [...]"}``), and the card read
"Resolved ask_user" with that raw JSON where the operator's answer belongs. A call whose result says it failed now goes to the ordinary tool block, which
carries the "failed" tag every other failed call has.

Runs the REAL ``NV_toolCallElement`` and ``NV_askAnsweredLine`` in V8 with the two card renderers stubbed to marker elements.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = DOC.index("function " + name + "(")
    return DOC[start:DOC.index("\n}\n", start) + len("\n}\n")]


_PRELUDE = r"""
function NV_AnsweredAskCard(props) { return React.createElement("div", { "data-testid": "answered-ask" }); }
function NV_ToolBlock(props) { return React.createElement("div", { "data-testid": "tool-block" }); }
"""


@pytest.fixture
def render():
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        code = bundler._transform(_function("NV_toolCallElement"), "snippet.jsx")
    finally:
        bundler._ctx.close()
    made = []

    def show(name: str, result: dict | None) -> str:
        ctx = mini_react_context(code, _PRELUDE)
        made.append(ctx)
        row = {"seq": 2, "kind": "tool_call", "payload": {"name": name, "arguments": {"prompt": "which one?"}, "id": "c1"}}
        ctx.eval(
            "function Probe() { return NV_toolCallElement(" + json.dumps(row) + ", " + json.dumps(result) + ", false); } MR.mount(Probe, {});"
        )
        return "answered-ask" if ctx.eval('MR.find("answered-ask") !== null') else ("tool-block" if ctx.eval('MR.find("tool-block") !== null') else "nothing")

    try:
        yield show
    finally:
        for ctx in made:
            ctx.close()


def _result(output, error: bool) -> dict:
    return {"seq": 3, "kind": "tool_result", "createdAt": "t3", "payload": {"call_id": "c1", "output": output, "error": error}}


def test_an_answered_ask_user_call_gets_the_answered_card(render) -> None:
    assert render("system__ask_user", _result(json.dumps({"response": "the blue one"}), False)) == "answered-ask"


def test_a_failed_ask_user_call_is_an_ordinary_failed_tool_block(render) -> None:
    validation = json.dumps({"type": "validation-error", "message": "argument validation failed: [{\"type\": \"missing\", \"loc\": [\"prompt\"]}]"})
    assert render("system__ask_user", _result(validation, True)) == "tool-block"


def test_a_still_pending_ask_user_call_keeps_the_tool_block(render) -> None:
    assert render("system__ask_user", None) == "tool-block"


def test_another_tool_with_a_result_is_unchanged(render) -> None:
    assert render("workspace__read_file", _result("contents", False)) == "tool-block"
    assert render("workspace__read_file", _result("boom", True)) == "tool-block"


def test_a_timed_out_or_cancelled_ask_is_still_an_answered_card_because_the_call_succeeded(render) -> None:
    """Those are terminal forms the tool returns on purpose (``error`` false), not failures of the call."""
    assert render("system__ask_user", _result(json.dumps({"timed_out": True, "elapsed_seconds": 30}), False)) == "answered-ask"
    assert render("system__ask_user", _result(json.dumps({"cancelled": True, "reason": "stopped"}), False)) == "answered-ask"
