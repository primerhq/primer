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
# a 404 gate_not_pending is not a replacement (decided elsewhere, the tool's own yield), so its words do not claim one; the 409 approval_stale keeps "replaced"
MOVED_ON_WORDS = "This approval has moved on; the list is reloaded."
REPLACED_WORDS = "This approval was replaced; the list is reloaded."
QUESTION_MOVED_ON_WORDS = "This question has moved on; the list is reloaded."


def _rendered(path: str, exc_factory) -> dict:
    """What the route at ``path`` answers when it raises ``exc_factory()``, through the REAL error handlers, as the console's ``ApiError`` carries it."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from primer.api.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)

    @app.post(path)
    def respond():
        raise exc_factory()

    answer = TestClient(app, raise_server_exceptions=False).post(path, json={})
    body = answer.json()
    return {"status": answer.status_code, "detail": body["detail"], "title": body["title"], "envelope": body}


@functools.cache
def not_pending_404() -> dict:
    """What ``POST /v1/sessions/<sid>/tool_approval/respond`` answers for a gate that is no longer pending: the route's ``GateNotPendingError``, a 404 with ``extensions.code = "gate_not_pending"``
    (the route itself is pinned by tests/api/test_gate_not_pending_code.py). After a graph ToolCall node's two-phase re-park the FIRST gate's id is pending nowhere, so a card drawn from it gets this and not
    the 409 ``approval_stale`` (board task 01a124e2-4550, ticket 01a125ec-68d6)."""
    from primer.api.gate_fence import GateNotPendingError

    got = _rendered("/v1/sessions/s-1/tool_approval/respond", lambda: GateNotPendingError("No pending tool_approval with tool_call_id 'tc-1' on 's-1'"))
    assert got["status"] == 404 and got["envelope"]["extensions"]["code"] == "gate_not_pending", got
    return got


@functools.cache
def question_not_pending_404() -> dict:
    """The ask_user respond route's answer for a question that is no longer pending (answered elsewhere, the graph asked again under a fresh id)."""
    from primer.api.gate_fence import GateNotPendingError

    got = _rendered("/v1/sessions/s-1/ask_user/respond",
                    lambda: GateNotPendingError("No pending ask_user prompt with tool_call_id 'tc-1' on session 's-1'"))
    assert got["status"] == 404 and got["envelope"]["extensions"]["code"] == "gate_not_pending", got
    return got


@functools.cache
def missing_session_404() -> dict:
    """The respond route's answer for a session that is not there: a plain ``NotFoundError``, no code. It is not a gate that moved on, so the console shows it as the error it is."""
    from primer.model.except_ import NotFoundError

    got = _rendered("/v1/sessions/s-1/tool_approval/respond", lambda: NotFoundError("'s-1' does not exist"))
    assert got["status"] == 404 and "code" not in (got["envelope"].get("extensions") or {}), got
    return got


# An unknown route (Starlette's own 404, the same type and title as the route's) and a proxy's 404 with a body that is not problem+json (ui/foundation/api.js: type about:blank).
ROUTE_404 = {"status": 404, "detail": "Not Found", "title": "Not Found",
             "envelope": {"type": "/errors/not-found", "title": "Not Found", "status": 404, "detail": "Not Found", "extensions": {"request_id": "req-1"}}}
PROXY_404 = {"status": 404, "detail": "Not Found", "title": "Not Found", "envelope": {"type": "about:blank", "title": "Not Found", "status": 404}}


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
    """The respond route answers 409 ``approval_stale`` when the id is pending under ANOTHER gate and 404 ``gate_not_pending`` when it is pending nowhere (the first gate of a node that re-parked). Both mean the
    card was drawn from a gate that has moved on. ``SH_isStaleGate`` stays the 409 alone."""
    moved = "SH_isMovedOnGate(" + json.dumps(not_pending_404()) + ")"
    assert _js(model, moved) is True
    assert _js(model, "SH_isMovedOnGate(" + json.dumps(STALE) + ")") is True
    assert _js(model, "SH_isMovedOnGate(" + json.dumps(OTHER_409) + ")") is False
    assert _js(model, "SH_isMovedOnGate(" + json.dumps(FAILED_500) + ")") is False
    assert _js(model, "SH_isMovedOnGate(null)") is False and _js(model, "SH_isMovedOnGate(new Error('boom'))") is False
    assert _js(model, "SH_isStaleGate(" + json.dumps(not_pending_404()) + ")") is False


def test_the_words_say_replaced_only_for_the_409_and_moved_on_for_a_404(model) -> None:
    """A 404 ``gate_not_pending`` is a gate pending under no approval gate: decided elsewhere ("has no pending tool_approval"), the node re-parked on the tool's own yield ("parked on a different tool"), the
    first gate of a two-phase re-park. Only the 409 ``approval_stale`` is a gate that was REPLACED, so only it says so."""

    def words(err: dict, kind: str = "approval") -> str:
        return _js(model, "SH_movedOnGateWords(" + json.dumps(err) + ", " + json.dumps(kind) + ")")

    assert words(STALE) == REPLACED_WORDS
    assert words(not_pending_404()) == MOVED_ON_WORDS
    assert words(not_pending_404(), "question") == "This question has moved on; the list is reloaded."
    assert words(STALE, "question") == "This question was replaced; the list is reloaded."
    assert REPLACED_WORDS != MOVED_ON_WORDS


def test_a_404_without_the_gate_not_pending_code_is_not_a_gate_that_moved_on(model) -> None:
    """Ticket 01a125ec-68d6: ``SH_isMovedOnGate`` matched ANY 404, so a deleted session, an unknown route or a proxy's 404 said "moved on" and reloaded a list that had not changed. Only the code the respond
    routes put on a gate that is no longer pending (and the 409 ``approval_stale``) is a gate that moved on; each code counts only with its own status."""
    for err in (missing_session_404(), ROUTE_404, PROXY_404, {"status": 404, "envelope": {"extensions": {"code": "approval_stale"}}}):
        assert _js(model, "SH_isMovedOnGate(" + json.dumps(err) + ")") is False, err
    assert _js(model, "SH_isMovedOnGate({status: 409, envelope: {extensions: {code: 'gate_not_pending'}}})") is False
    assert _js(model, "SH_isMovedOnGate(" + json.dumps(not_pending_404()) + ")") is True
    assert _js(model, "SH_isMovedOnGate(" + json.dumps(question_not_pending_404()) + ")") is True


def test_the_code_is_read_by_the_one_reader_of_a_failed_request() -> None:
    """With ui/foundation/api.js loaded, as the console loads it before this file, the code comes from ``window.primerApi.readRefusal``: the same answers, and a code the reader finds outside
    ``extensions.code`` (``extensions.error``) counts too."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        ctx.eval("var window = globalThis;")
        ctx.eval((ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8"))
        ctx.eval(ATTENTION)
        ctx.eval("var READS = 0; var _read = window.primerApi.readRefusal; window.primerApi.readRefusal = function (e, f, o) { READS += 1; return _read(e, f, o); };")
        assert _js(ctx, "SH_isMovedOnGate(" + json.dumps(not_pending_404()) + ")") is True
        assert _js(ctx, "SH_isMovedOnGate(" + json.dumps(STALE) + ")") is True
        assert _js(ctx, "SH_isMovedOnGate(" + json.dumps(missing_session_404()) + ")") is False
        assert _js(ctx, "SH_isMovedOnGate(" + json.dumps(ROUTE_404) + ")") is False
        assert _js(ctx, "SH_isMovedOnGate({status: 404, envelope: {extensions: {error: 'gate_not_pending'}}})") is True
        assert _js(ctx, "READS") > 0, "the predicate read the code through readRefusal"
    finally:
        ctx.close()


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
var KINDS = [];
var NEXT = { fail: null };
function NV_useConsole() { return { username: "reviewer", role: "admin", toast: function (m, extra) { TOASTS.push(m); KINDS.push((extra && extra.kind) || null); } }; }
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
    assert _js(decision, "TOASTS") == [MOVED_ON_WORDS]
    assert _js(decision, "RESOLVED") == [1], "the pending list is reloaded"


def test_a_reject_for_a_gate_that_is_pending_nowhere_says_it_moved_on_and_reloads_the_list(decision) -> None:
    decision.eval(
        "NEXT.fail = " + json.dumps(not_pending_404()) + ";"
        "MR.click('nv-reject'); MR.find('nv-reject-reason').props.onChange({ target: { value: 'no' } }); MR.rerender(); MR.click('nv-reject');")
    assert _js(decision, "TOASTS") == [MOVED_ON_WORDS]
    assert _js(decision, "RESOLVED") == [1]


def _reject(ctx) -> None:
    ctx.eval("MR.click('nv-reject'); MR.find('nv-reject-reason').props.onChange({ target: { value: 'no' } }); MR.rerender(); MR.click('nv-reject');")


@pytest.mark.parametrize("act", ["approve", "reject"])
@pytest.mark.parametrize("err,words", [(STALE, REPLACED_WORDS), ("not_pending", MOVED_ON_WORDS)])
def test_a_gate_that_moved_on_is_a_warning_toast_like_the_banner_and_the_inbox(act, err, words) -> None:
    """Ticket 01a125ec-68d6: one wording and one toast kind for a moved-on gate on every surface; the desktop card toasted it with the default kind, the banner as a warning and the Inbox as an error."""
    ctx = _card("NV_DecisionCard", _APPROVAL)
    try:
        ctx.eval("NEXT.fail = " + json.dumps(not_pending_404() if err == "not_pending" else err) + ";")
        if act == "approve":
            ctx.eval("MR.click('nv-approve');")
        else:
            _reject(ctx)
        assert _js(ctx, "TOASTS") == [words]
        assert _js(ctx, "KINDS") == ["warning"]
        assert _js(ctx, "RESOLVED") == [1]
    finally:
        ctx.close()


@pytest.mark.parametrize("err", ["missing_session", "route"])
def test_an_approve_or_reject_answered_by_a_404_without_the_code_is_the_error_it_is_and_does_not_reload(decision, err) -> None:
    """A deleted session (or an unknown route) is not a gate that moved on: it used to say "moved on" and reload, which hid the real failure."""
    fail, detail = (missing_session_404(), "'s-1' does not exist") if err == "missing_session" else (ROUTE_404, "Not Found")
    decision.eval("NEXT.fail = " + json.dumps(fail) + "; MR.click('nv-approve');")
    _reject(decision)
    assert _js(decision, "TOASTS") == ["Approve failed: " + detail, "Reject failed: " + detail]
    assert _js(decision, "RESOLVED") == []


def test_a_server_failure_on_a_reject_is_still_a_failure_and_does_not_reload(decision) -> None:
    """The reject arm has its own predicate: a reject that treated every failure as moved on would say so for a 500 and reload a list that has not changed."""
    decision.eval(
        "NEXT.fail = " + json.dumps(FAILED_500) + ";"
        "MR.click('nv-reject'); MR.find('nv-reject-reason').props.onChange({ target: { value: 'no' } }); MR.rerender(); MR.click('nv-reject');")
    assert _js(decision, "TOASTS") == ["Reject failed: the store is down"]
    assert _js(decision, "RESOLVED") == []


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


def test_an_answer_for_a_question_that_moved_on_says_so_inline_in_the_shared_words_and_any_other_404_is_the_error(ask) -> None:
    """Ticket 01a125ec-68d6: the question card showed the raw "No pending ask_user prompt ..." inline and left the list as it was. A 404 ``gate_not_pending`` now reads like the 409: the shared words, and
    the list reloads. A deleted session is the error it is: its own words, no reload."""
    ask.eval("NEXT.fail = " + json.dumps(question_not_pending_404()) + "; MR.find('nv-ask-answer').props.onChange({ target: { value: 'EUR' } }); MR.rerender(); MR.click('nv-ask-submit');")
    ask.eval("MR.rerender();")
    assert _js(ask, "MR.find('nv-ask-error').props.children") == QUESTION_MOVED_ON_WORDS
    assert _js(ask, "RESOLVED") == [1]
    ask.eval("NEXT.fail = " + json.dumps(missing_session_404()) + "; MR.click('nv-ask-submit');")
    ask.eval("MR.rerender();")
    assert _js(ask, "MR.find('nv-ask-error').props.children") == "'s-1' does not exist"
    assert _js(ask, "RESOLVED") == [1], "a 404 without the code does not reload"


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


@pytest.mark.parametrize("err,words", [(STALE, REPLACED_WORDS), ("not_pending", MOVED_ON_WORDS)])
def test_an_inbox_card_whose_gate_moved_on_says_so_in_the_shared_words_as_a_warning(inbox, err, words) -> None:
    """Ticket 01a125ec-68d6: the Inbox said a replaced gate in the desktop's words but as an ERROR, and a 404 in words of its own ("That approval has moved on. Open the session ..."), also an error."""
    inbox.eval("NEXT.fail = " + json.dumps(not_pending_404() if err == "not_pending" else err) + ";")
    got = _decide(inbox, "approve")
    assert got["result"] == {"stale": True} and got["resolved"] == 1
    assert got["toasts"] == [[words, {"kind": "warning", "requestId": None}]]


def test_an_inbox_decision_answered_by_a_404_without_the_code_is_the_error_it_is(inbox) -> None:
    """A deleted session is not a gate that moved on: an error toast with the server's words and the request id, and nothing is refreshed."""
    inbox.eval("NEXT.fail = " + json.dumps({**missing_session_404(), "requestId": "req-9"}) + ";")
    got = _decide(inbox, "deny")
    assert got["result"] == {"failed": True} and got["resolved"] == 0
    assert got["toasts"] == [["Deny failed: 's-1' does not exist", {"kind": "error", "requestId": "req-9"}]]
