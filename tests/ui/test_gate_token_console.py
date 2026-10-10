"""The console names the gate it decides, and says so in words when that gate was replaced (console review C-033, ticket 01a11f52-9d98).

A provider repeats its tool_call_id across rounds, so a card left open for round 1's approval used to decide whatever gate was pending under the
same id later. The server now serves a ``gate_id`` per gate and refuses a decision naming a replaced one with a 409 ``approval_stale``. Here:

* the attention items carry the id the row served (``gateId``);
* ``SH_api.approve`` / ``reject`` / ``answer`` send it back as ``gate_id`` (and only when there is one: a row from before gates had ids sends none);
* the desktop decision card, the ask card and the mobile Inbox pass it, and on a stale refusal say "This approval was replaced; the list is reloaded."
  (or "question") and reload the pending list instead of showing a failure.

The real ``NV_DecisionCard`` / ``NV_AskCard`` run in V8 on the hook runtime in ``tests/ui/_mini_react.py``; ``SH_api`` records what would have been sent.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
ATTENTION = (ROOT / "ui" / "foundation" / "shell-attention.js").read_text(encoding="utf-8")
DOC = ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx"
SH_API = ROOT / "ui" / "components" / "shell" / "sh-api.jsx"
MOBILE = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")

STALE = {"status": 409, "detail": "this approval was replaced by a newer one; reload the pending list",
         "envelope": {"extensions": {"code": "approval_stale"}}}
OTHER_409 = {"status": 409, "detail": "Session 'sess-1' changed state", "envelope": {"extensions": {}}}
FAILED_500 = {"status": 500, "detail": "the store is down", "envelope": {"extensions": {}}}


@functools.cache
def not_pending_404() -> dict:
    """What ``POST /v1/sessions/<sid>/tool_approval/respond`` answers for a gate that is no longer pending (``NotFoundError`` through the real error handlers), as the console's ``ApiError`` carries it. After a graph
    ToolCall node's two-phase re-park the FIRST gate's id is pending nowhere, so a card drawn from it gets this and not the 409 ``approval_stale`` (board task 01a124e2-4550)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from primer.api.errors import register_error_handlers
    from primer.model.except_ import NotFoundError

    app = FastAPI()
    register_error_handlers(app)

    @app.post("/v1/sessions/s-1/tool_approval/respond")
    def respond():
        raise NotFoundError("No pending tool_approval with tool_call_id 'tc-1' on 's-1'")

    answer = TestClient(app, raise_server_exceptions=False).post("/v1/sessions/s-1/tool_approval/respond", json={})
    assert answer.status_code == 404, answer.text
    body = answer.json()
    return {"status": answer.status_code, "detail": body["detail"], "envelope": body}


def _js(ctx, expression: str):
    return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))


# ---- the attention model --------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def model():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(ATTENTION)
    try:
        yield ctx
    finally:
        ctx.close()


def test_the_attention_item_carries_the_gate_id_the_row_served(model) -> None:
    rows = [
        {"session_id": "s-1", "kind": "approval", "tool_call_id": "tc-1", "gate_id": "a" * 32, "prompt": "p", "parked_at": "2026-10-09T10:00:00Z"},
        {"session_id": "s-2", "kind": "approval", "tool_call_id": "tc-2", "prompt": "p", "parked_at": "2026-10-09T10:00:00Z"},
    ]
    items = _js(model, "SH_toAttentionItems({pending: " + json.dumps(rows) + ", records: []})")
    assert [i["gateId"] for i in items] == ["a" * 32, None]


def test_a_gate_that_is_pending_nowhere_is_moved_on_like_a_replaced_one_and_a_failure_is_not(model) -> None:
    """The respond route answers 409 ``approval_stale`` when the id is pending under ANOTHER gate and 404 when it is pending nowhere (the first gate of a node that re-parked). Both mean the card was drawn from a
    gate that has moved on. ``SH_isStaleGate`` stays the 409 alone: the question cards and the session detail read it."""
    moved = "SH_isMovedOnGate(" + json.dumps(not_pending_404()) + ")"
    assert _js(model, moved) is True
    assert _js(model, "SH_isMovedOnGate(" + json.dumps(STALE) + ")") is True
    assert _js(model, "SH_isMovedOnGate(" + json.dumps(OTHER_409) + ")") is False
    assert _js(model, "SH_isMovedOnGate(" + json.dumps(FAILED_500) + ")") is False
    assert _js(model, "SH_isMovedOnGate(null)") is False and _js(model, "SH_isMovedOnGate(new Error('boom'))") is False
    assert _js(model, "SH_isStaleGate(" + json.dumps(not_pending_404()) + ")") is False


def test_a_409_with_the_stale_code_is_a_stale_gate_and_nothing_else_is(model) -> None:
    assert _js(model, "SH_isStaleGate(" + json.dumps(STALE) + ")") is True
    assert _js(model, "SH_isStaleGate(" + json.dumps(OTHER_409) + ")") is False
    assert _js(model, "SH_isStaleGate({status: 404, envelope: {extensions: {code: 'approval_stale'}}})") is False
    assert _js(model, "SH_isStaleGate(null)") is False
    assert _js(model, "SH_isStaleGate(new Error('boom'))") is False


def test_the_stale_words_name_what_was_replaced_and_say_the_list_is_reloaded(model) -> None:
    assert _js(model, "SH_staleGateWords('approval')") == "This approval was replaced; the list is reloaded."
    assert _js(model, "SH_staleGateWords('question')") == "This question was replaced; the list is reloaded."


# ---- SH_api ---------------------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def api():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; var CALLS = [];"
             "window.primerApi = { apiFetch: function (method, path, body) { CALLS.push([method, path, body]); return Promise.resolve({}); } };")
    ctx.eval(transpile(SH_API))
    try:
        yield ctx
    finally:
        ctx.close()


def test_approve_reject_and_answer_send_the_gate_id_back(api) -> None:
    api.eval("SH_api.approve('s-1', 'tc-1', '" + "a" * 32 + "'); SH_api.reject('s-1', 'tc-1', 'no', '" + "b" * 32 + "');"
             "SH_api.answer('s-1', 'tc-1', 'EUR', '" + "c" * 32 + "');")
    assert _js(api, "CALLS") == [
        ["POST", "/sessions/s-1/tool_approval/respond", {"tool_call_id": "tc-1", "decision": "approved", "gate_id": "a" * 32}],
        ["POST", "/sessions/s-1/tool_approval/respond", {"tool_call_id": "tc-1", "decision": "rejected", "reason": "no", "gate_id": "b" * 32}],
        ["POST", "/sessions/s-1/ask_user/respond", {"tool_call_id": "tc-1", "response": "EUR", "gate_id": "c" * 32}],
    ]


def test_a_gate_without_an_id_sends_no_gate_id_field(api) -> None:
    """A row from before gates had ids: the body is exactly what it was."""
    api.eval("SH_api.approve('s-1', 'tc-1'); SH_api.reject('s-1', 'tc-1', 'no', null); SH_api.answer('s-1', 'tc-1', 'EUR', undefined);")
    assert [c[2] for c in _js(api, "CALLS")] == [
        {"tool_call_id": "tc-1", "decision": "approved"},
        {"tool_call_id": "tc-1", "decision": "rejected", "reason": "no"},
        {"tool_call_id": "tc-1", "response": "EUR"},
    ]


# ---- the desktop cards ----------------------------------------------------------------------------------------------------------------------------

_PRELUDE = r"""
var window = globalThis;
var TOASTS = [];
var REJECTS = [];
var APPROVES = [];
var ANSWERS = [];
var RESOLVED = [];
var NEXT = { fail: null };
function NV_useConsole() { return { username: "reviewer", role: "admin", toast: function (m) { TOASTS.push(m); } }; }
function SH_looksLikeDiff() { return false; }
function SH_diffLineTone() { return "ctx"; }
function __settle(call) { return NEXT.fail ? Promise.reject(NEXT.fail) : Promise.resolve({}); }
var SH_api = {
  approve: function (sid, tc, gate) { APPROVES.push([sid, tc, gate === undefined ? null : gate]); return __settle(); },
  reject: function (sid, tc, reason, gate) { REJECTS.push([sid, tc, reason, gate === undefined ? null : gate]); return __settle(); },
  answer: function (sid, tc, val, gate) { ANSWERS.push([sid, tc, val, gate === undefined ? null : gate]); return __settle(); },
};
"""


def _card(component: str, item: dict):
    ctx = mini_react_context(transpile(DOC), "var window = globalThis;\n" + ATTENTION + "\n" + _PRELUDE)
    ctx.eval(
        f"MR.mount({component}, {{ item: {json.dumps(item)}, ended: false, live: true, onResolved: function () {{ RESOLVED.push(1); }} }});"
    )
    return ctx


_APPROVAL = {"toolCallId": "tc-1", "sessionId": "s-1", "gatedTool": "write", "preview": "", "gateId": "a" * 32}
_QUESTION = {"toolCallId": "tc-1", "sessionId": "s-1", "title": "Which currency?", "preview": "Which currency?", "gateId": "a" * 32}


@pytest.fixture
def decision():
    ctx = _card("NV_DecisionCard", _APPROVAL)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def ask():
    ctx = _card("NV_AskCard", _QUESTION)
    try:
        yield ctx
    finally:
        ctx.close()


def test_approve_and_reject_name_the_gate_the_card_was_drawn_from(decision) -> None:
    decision.eval("MR.click('nv-approve');")
    assert _js(decision, "APPROVES") == [["s-1", "tc-1", "a" * 32]]
    decision.eval("MR.click('nv-reject'); MR.find('nv-reject-reason').props.onChange({ target: { value: 'no' } }); MR.rerender(); MR.click('nv-reject');")
    assert _js(decision, "REJECTS") == [["s-1", "tc-1", "no", "a" * 32]]


def test_a_stale_approve_says_so_in_words_and_reloads_the_list(decision) -> None:
    decision.eval("NEXT.fail = " + json.dumps(STALE) + "; MR.click('nv-approve');")
    assert _js(decision, "TOASTS") == ["This approval was replaced; the list is reloaded."]
    assert _js(decision, "RESOLVED") == [1], "the pending list is reloaded"


def test_a_stale_reject_says_so_in_words_and_reloads_the_list(decision) -> None:
    decision.eval(
        "NEXT.fail = " + json.dumps(STALE) + ";"
        "MR.click('nv-reject'); MR.find('nv-reject-reason').props.onChange({ target: { value: 'no' } }); MR.rerender(); MR.click('nv-reject');")
    assert _js(decision, "TOASTS") == ["This approval was replaced; the list is reloaded."]
    assert _js(decision, "RESOLVED") == [1]


def test_an_approve_for_a_gate_that_is_pending_nowhere_says_it_moved_on_and_reloads_the_list(decision) -> None:
    """Board task 01a124e2-4550: the card used to toast the raw ``Approve failed: No pending tool_approval ...`` and leave the list as it was."""
    decision.eval("NEXT.fail = " + json.dumps(not_pending_404()) + "; MR.click('nv-approve');")
    assert _js(decision, "TOASTS") == ["This approval was replaced; the list is reloaded."]
    assert _js(decision, "RESOLVED") == [1], "the pending list is reloaded"


def test_a_reject_for_a_gate_that_is_pending_nowhere_says_it_moved_on_and_reloads_the_list(decision) -> None:
    decision.eval(
        "NEXT.fail = " + json.dumps(not_pending_404()) + ";"
        "MR.click('nv-reject'); MR.find('nv-reject-reason').props.onChange({ target: { value: 'no' } }); MR.rerender(); MR.click('nv-reject');")
    assert _js(decision, "TOASTS") == ["This approval was replaced; the list is reloaded."]
    assert _js(decision, "RESOLVED") == [1]


def test_a_server_failure_is_still_a_failure_and_does_not_reload(decision) -> None:
    decision.eval("NEXT.fail = " + json.dumps(FAILED_500) + "; MR.click('nv-approve');")
    assert _js(decision, "TOASTS") == ["Approve failed: the store is down"]
    assert _js(decision, "RESOLVED") == []


def test_any_other_failure_is_still_a_failure_and_does_not_reload(decision) -> None:
    decision.eval("NEXT.fail = " + json.dumps(OTHER_409) + "; MR.click('nv-approve');")
    assert _js(decision, "TOASTS") == ["Approve failed: Session 'sess-1' changed state"]
    assert _js(decision, "RESOLVED") == []


def test_the_ask_card_answers_with_its_gate_id(ask) -> None:
    ask.eval("MR.find('nv-ask-answer').props.onChange({ target: { value: 'EUR' } }); MR.rerender(); MR.click('nv-ask-submit');")
    assert _js(ask, "ANSWERS") == [["s-1", "tc-1", "EUR", "a" * 32]]


def test_a_stale_answer_says_so_inline_and_reloads_the_list(ask) -> None:
    ask.eval("NEXT.fail = " + json.dumps(STALE) + "; MR.find('nv-ask-answer').props.onChange({ target: { value: 'EUR' } }); MR.rerender(); MR.click('nv-ask-submit');")
    ask.eval("MR.rerender();")          # the refusal lands in a promise callback, after the click returned
    assert _js(ask, "MR.find('nv-ask-error').props.children") == "This question was replaced; the list is reloaded."
    assert _js(ask, "RESOLVED") == [1]


# ---- the mobile Inbox -----------------------------------------------------------------------------------------------------------------------------

_INBOX_STUBS = r"""
var window = globalThis;
var CALLS = [];
var TOASTS = [];
var RESOLVED = 0;
var NEXT = { fail: null };
function toast(msg, extra) { TOASTS.push([msg, extra || null]); }
function onResolved() { RESOLVED += 1; }
var SH_api = {
  approve: function (sid, tcid, gate) { CALLS.push(["approve", sid, tcid, gate === undefined ? null : gate]); return NEXT.fail ? Promise.reject(NEXT.fail) : Promise.resolve({}); },
  reject: function (sid, tcid, reason, gate) { CALLS.push(["reject", sid, tcid, reason, gate === undefined ? null : gate]); return NEXT.fail ? Promise.reject(NEXT.fail) : Promise.resolve({}); },
};
var RESULT = null;
"""


@pytest.fixture
def inbox():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_INBOX_STUBS)
    ctx.eval(ATTENTION)
    start = MOBILE.index("function NV_inboxDecide")
    ctx.eval(MOBILE[start:MOBILE.index("\n}\n", start) + len("\n}\n")])
    try:
        yield ctx
    finally:
        ctx.close()


_ROW = {"workspace_id": "w1", "session_id": "sess-1", "kind": "approval", "tool_call_id": "tc-1", "gate_id": "a" * 32,
        "approval": {"tool_name": "write", "arguments": "path=a", "truncated": False}}


def _decide(ctx, decision: str) -> dict:
    ctx.eval("RESULT = null; NV_inboxDecide(" + json.dumps(decision) + ", " + json.dumps(_ROW) + ", toast, onResolved).then(function (r) { RESULT = r; });")
    return {"result": _js(ctx, "RESULT"), "calls": _js(ctx, "CALLS"), "toasts": _js(ctx, "TOASTS"), "resolved": ctx.eval("RESOLVED")}


def test_the_inbox_decision_names_the_gate_the_row_served(inbox) -> None:
    assert _decide(inbox, "approve")["calls"] == [["approve", "sess-1", "tc-1", "a" * 32]]
    inbox.eval("CALLS = [];")
    assert _decide(inbox, "deny")["calls"] == [["reject", "sess-1", "tc-1", "", "a" * 32]]


def test_a_stale_inbox_card_says_it_was_replaced_and_reloads(inbox) -> None:
    inbox.eval("NEXT.fail = " + json.dumps(STALE) + ";")
    got = _decide(inbox, "approve")
    assert got["result"] == {"stale": True} and got["resolved"] == 1
    assert [t[0] for t in got["toasts"]] == ["This approval was replaced; the list is reloaded."]
