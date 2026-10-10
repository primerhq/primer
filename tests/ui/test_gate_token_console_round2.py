"""The console, round 2 of the C-033 review: cards are keyed by the gate, the cancel buttons name the yield they cancel, and the legacy banner and
panel are pinned (console review C-033, ticket 01a11f52-9d98).

* A card is keyed by the GATE (``pending:<session>:<gate id>``, the raw tool_call_id only for a row from before gates had ids), so a gate replaced
  under the same provider id mounts a fresh card: what was typed into the old card (an answer, a rejection reason) does not survive the 409 reload.
* ``CancelYieldBtn`` (the watch_files and sleep panels) and the external-tools cancel send ``expected_tool_name``, so the server can refuse a Skip
  left open for one kind of yield when the raw id now belongs to another.
* The legacy ``ApprovalBanner`` and ``AskUserPanel`` send the gate id and say so in words when it was replaced.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.ui._mini_react import mini_react_context, transpile
from tests.ui.test_gate_token_console import _PRELUDE, ATTENTION, DOC, FAILED_500, ROOT, STALE, _js, not_pending_404

SESSION_DETAIL = (ROOT / "ui" / "components" / "session-detail.jsx").read_text(encoding="utf-8")
APPROVALS = (ROOT / "ui" / "components" / "approvals.jsx").read_text(encoding="utf-8")
EXTERNAL_TOOLS = (ROOT / "ui" / "components" / "external-tools.jsx").read_text(encoding="utf-8")
PLATFORM = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
MOBILE = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")

G1 = "a" * 32
G2 = "b" * 32


def _transpile_source(source: str, name: str) -> str:
    """``tests.ui._mini_react.transpile`` takes a file; the legacy components live inside big files, so the function under test is cut out first."""
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform(source, name)
    finally:
        bundler._ctx.close()


def _row(session: str, tcid: str, gate: str | None) -> dict:
    row = {"session_id": session, "kind": "ask_user", "tool_call_id": tcid, "prompt": "Which currency?", "parked_at": "2026-10-09T10:00:00Z"}
    if gate:
        row["gate_id"] = gate
    return row


# ---- card keys ------------------------------------------------------------------------------------------------------------------------------------


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


def _ids(ctx, rows: list[dict]) -> list[str]:
    return [i["id"] for i in _js(ctx, "SH_toAttentionItems({pending: " + json.dumps(rows) + ", records: []})")]


def test_two_gates_under_one_raw_id_have_two_ids(model) -> None:
    ids = _ids(model, [_row("s-1", "call_0", G1), _row("s-1", "call_0", G2)])
    assert ids == [f"pending:s-1:{G1}", f"pending:s-1:{G2}"]


def test_a_row_from_before_gates_had_ids_is_keyed_by_session_and_raw_id(model) -> None:
    assert _ids(model, [_row("s-1", "call_0", None)]) == ["pending:s-1:call_0"]


def test_the_same_raw_id_in_two_sessions_is_two_cards(model) -> None:
    assert len(set(_ids(model, [_row("s-1", "call_0", None), _row("s-2", "call_0", None)]))) == 2


def test_the_platform_rows_use_the_same_key(model) -> None:
    """The approvals table in the platform page keyed its pending rows by the raw id alone, so two sessions' rows (or two rounds) collided."""
    assert re.search(r'"pending:"\s*\+\s*rec\.tool_call_id', PLATFORM) is None
    assert "SH_pendingId(" in PLATFORM
    assert _js(model, "SH_pendingId('s-1', '" + G1 + "', 'call_0')") == f"pending:s-1:{G1}"
    assert _js(model, "SH_pendingId('s-1', null, 'call_0')") == "pending:s-1:call_0"


_LIST = r"""
function GateList(props) {
  return React.createElement("div", null, props.items.map(function (it) {
    return React.createElement(NV_AskCard, { key: it.id, item: it, ended: false, onResolved: function () { RESOLVED.push(1); } });
  }));
}
"""


def _items(*rows: dict) -> str:
    return "SH_toAttentionItems({pending: " + json.dumps(list(rows)) + ", records: []})"


def _typed_answer(ctx) -> str:
    return ctx.eval("MR.find('nv-ask-answer').props.value")


@pytest.fixture
def list_ctx():
    ctx = mini_react_context(transpile(DOC), "var window = globalThis;\n" + ATTENTION + "\n" + _PRELUDE + _LIST)
    try:
        yield ctx
    finally:
        ctx.close()


def test_what_was_typed_into_a_replaced_gates_card_does_not_survive_the_reload(list_ctx) -> None:
    """Round 1's question stayed open with 'EUR' typed into it; the 409 reload brings round 3's question under the SAME raw id. It is a new card."""
    list_ctx.eval("MR.mount(GateList, { items: " + _items(_row("s-1", "call_0", G1)) + " });")
    list_ctx.eval("MR.find('nv-ask-answer').props.onChange({ target: { value: 'EUR' } }); MR.rerender();")
    assert _typed_answer(list_ctx) == "EUR"

    list_ctx.eval("MR.rerender({ items: " + _items(_row("s-1", "call_0", G2)) + " });")

    assert _typed_answer(list_ctx) == "", "the new gate's card inherited the old card's draft"


def test_what_was_typed_survives_a_refresh_of_the_same_gate(list_ctx) -> None:
    """The control: the pending list is polled, and a poll that returns the same gate must not wipe a draft."""
    list_ctx.eval("MR.mount(GateList, { items: " + _items(_row("s-1", "call_0", G1)) + " });")
    list_ctx.eval("MR.find('nv-ask-answer').props.onChange({ target: { value: 'EUR' } }); MR.rerender();")

    list_ctx.eval("MR.rerender({ items: " + _items(_row("s-1", "call_0", G1)) + " });")

    assert _typed_answer(list_ctx) == "EUR"


# ---- expected_tool_name ---------------------------------------------------------------------------------------------------------------------------

_CANCEL_STUBS = r"""
var window = globalThis;
var CALLS = [];
function Btn(p) { return React.createElement("button", { "data-testid": "cancel-yield", onClick: p.onClick }, p.children); }
function _sdToastErr() { return function () {}; }
window.primerApi = {
  apiFetch: function (method, path, body) { CALLS.push([method, path, body]); return Promise.resolve({}); },
  useMutation: function (fn) { return { loading: false, mutate: function () { return fn(); } }; },
};
"""


def _cancel_yield_btn_source() -> str:
    start = SESSION_DETAIL.index("function CancelYieldBtn")
    return SESSION_DETAIL[start:SESSION_DETAIL.index("\n}\n", start) + 3]


@pytest.mark.parametrize("tool", ["watch_files", "sleep"])
def test_cancel_yield_btn_sends_the_tool_it_was_drawn_for(tool) -> None:
    ctx = mini_react_context(_transpile_source(_cancel_yield_btn_source(), 'cancel-yield-btn.jsx'), _CANCEL_STUBS)
    try:
        ctx.eval("MR.mount(CancelYieldBtn, { sid: 's-1', wid: 'w1', tcid: 'call_0', toolName: " + json.dumps(tool) + " });")
        ctx.eval("MR.click('cancel-yield');")
        assert _js(ctx, "CALLS") == [[
            "POST", "/sessions/s-1/yields/call_0/cancel", {"reason": "operator cancelled", "expected_tool_name": tool},
        ]]
    finally:
        ctx.close()


def test_the_watch_files_and_sleep_panels_pass_their_tool_name() -> None:
    assert re.search(r'<CancelYieldBtn[^>]*toolName="watch_files"', SESSION_DETAIL)
    assert re.search(r'<CancelYieldBtn[^>]*toolName="sleep"', SESSION_DETAIL)


def test_the_external_tools_cancel_names_its_kind() -> None:
    start = EXTERNAL_TOOLS.index("async function cancel(tcid)")
    body = EXTERNAL_TOOLS[start:EXTERNAL_TOOLS.index("\n    }\n", start)]
    assert 'expected_tool_name: "_external"' in body


# ---- the legacy approval banner ------------------------------------------------------------------------------------------------------------------

_BANNER_STUBS = r"""
var window = globalThis;
var CALLS = [];
var TOASTS = [];
var NEXT = { fail: null };
function Icon() { return null; }
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], onClick: p.onClick }, p.children); }
window.primerApi = {
  apiFetch: function (method, path, body) { CALLS.push([method, path, body]); return NEXT.fail ? Promise.reject(NEXT.fail) : Promise.resolve({}); },
  useMutation: function (fn, opts) {
    return { loading: false, mutate: function (body) {
      return fn(body).then(function () { if (opts.onSuccess) opts.onSuccess(); }, function (e) { if (opts.onError) opts.onError(e); });
    } };
  },
};
function pushToast(t) { TOASTS.push(t); }
"""


def _banner_source() -> str:
    start = APPROVALS.index("function AP_toastErr")
    toast = APPROVALS[start:APPROVALS.index("\n}\n", start) + 3]
    start = APPROVALS.index("function ApprovalBanner")
    return toast + APPROVALS[start:APPROVALS.index("\n}\n", start) + 3]


def _banner(data: dict):
    ctx = mini_react_context(_transpile_source(_banner_source(), 'approval-banner.jsx'), _BANNER_STUBS + ATTENTION)
    ctx.eval("MR.mount(ApprovalBanner, { data: " + json.dumps(data) + ", scope: 'sessions', id: 's-1', pushToast: pushToast });")
    return ctx


_BANNER = {"tool_call_id": "call_0", "tool_name": "write", "arguments": {}, "gate_id": G1}


def test_the_banner_approves_with_the_gate_it_was_drawn_from() -> None:
    ctx = _banner(_BANNER)
    try:
        ctx.eval("MR.click('approval-banner-approve');")
        assert _js(ctx, "CALLS") == [[
            "POST", "/sessions/s-1/tool_approval/respond", {"tool_call_id": "call_0", "decision": "approved", "gate_id": G1},
        ]]
    finally:
        ctx.close()


def test_the_banner_rejects_with_the_gate_it_was_drawn_from() -> None:
    ctx = _banner(_BANNER)
    try:
        ctx.eval("MR.click('approval-banner-reject');")
        ctx.eval("MR.find('approval-banner-reason').props.onChange({ target: { value: 'no' } }); MR.rerender(); MR.click('approval-banner-reject-submit');")
        assert _js(ctx, "CALLS")[0][2] == {"tool_call_id": "call_0", "decision": "rejected", "reason": "no", "gate_id": G1}
    finally:
        ctx.close()


def test_a_banner_without_a_gate_id_sends_none() -> None:
    ctx = _banner({k: v for k, v in _BANNER.items() if k != "gate_id"})
    try:
        ctx.eval("MR.click('approval-banner-approve');")
        assert _js(ctx, "CALLS")[0][2] == {"tool_call_id": "call_0", "decision": "approved"}
    finally:
        ctx.close()


def test_a_stale_banner_says_so_in_words_and_is_not_an_error_toast() -> None:
    ctx = _banner(_BANNER)
    try:
        ctx.eval("NEXT.fail = " + json.dumps(STALE) + "; MR.click('approval-banner-approve');")
        ctx.eval("MR.rerender();")
        assert _js(ctx, "TOASTS") == [{"kind": "warning", "title": "This approval was replaced; the list is reloaded."}]
    finally:
        ctx.close()


def test_a_banner_for_a_gate_that_is_pending_nowhere_says_it_moved_on_and_is_not_an_error_toast() -> None:
    """Board task 01a124e2-4550: after a graph node's two-phase re-park the first gate's id is pending nowhere (404, not 409 ``approval_stale``)."""
    ctx = _banner(_BANNER)
    try:
        ctx.eval("NEXT.fail = " + json.dumps(not_pending_404()) + "; MR.click('approval-banner-approve');")
        ctx.eval("MR.rerender();")
        assert _js(ctx, "TOASTS") == [{"kind": "warning", "title": "This approval was replaced; the list is reloaded."}]
    finally:
        ctx.close()


def test_a_server_failure_is_still_an_error_toast_on_the_banner() -> None:
    ctx = _banner(_BANNER)
    try:
        ctx.eval("NEXT.fail = " + json.dumps(FAILED_500) + "; MR.click('approval-banner-approve');")
        ctx.eval("MR.rerender();")
        toasts = _js(ctx, "TOASTS")
        assert len(toasts) == 1 and toasts[0]["kind"] == "error" and "Respond failed" in toasts[0]["title"], toasts
    finally:
        ctx.close()


# ---- the legacy ask_user panel -------------------------------------------------------------------------------------------------------------------

_PANEL_STUBS = r"""
var window = globalThis;
var CALLS = [];
var TOASTS = [];
var REFETCH = [];
var NEXT = { fail: null };
var SESSION_TERMINAL = new Set();
var PENDING = { data: null, error: null };
function Icon() { return null; }
function fmtDate(d) { return String(d); }
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], onClick: p.onClick }, p.children); }
window.SessionCountdown = function () { return null; };
window.primerApi = {
  apiFetch: function (method, path, body) { CALLS.push([method, path, body]); return NEXT.fail ? Promise.reject(NEXT.fail) : Promise.resolve({}); },
  useResource: function () { return { data: PENDING.data, error: PENDING.error, refetch: function () { REFETCH.push(1); } }; },
};
function pushToast(t) { TOASTS.push(t); }
"""


def _panel_source() -> str:
    start = SESSION_DETAIL.index("function AskUserPanel")
    return SESSION_DETAIL[start:SESSION_DETAIL.index("\n}\n", start) + 3]


def _panel(pending: dict):
    ctx = mini_react_context(_transpile_source(_panel_source(), 'ask-user-panel.jsx'), _PANEL_STUBS + ATTENTION)
    ctx.eval("PENDING.data = " + json.dumps(pending) + ";")
    ctx.eval("MR.mount(AskUserPanel, { sid: 's-1', sessionStatus: 'waiting', session: null, pushToast: pushToast });")
    return ctx


_PENDING = {"tool_call_id": "call_0", "prompt": "Which currency?", "response_schema": None, "gate_id": G1, "parked_at": "2026-10-09T10:00:00Z"}


def _answer(ctx) -> None:
    ctx.eval("MR.find('ask-user-input').props.onChange({ target: { value: 'EUR' } }); MR.rerender(); MR.click('ask-user-submit');")


def test_the_panel_answers_with_the_gate_it_was_drawn_from() -> None:
    ctx = _panel(_PENDING)
    try:
        _answer(ctx)
        assert _js(ctx, "CALLS") == [[
            "POST", "/sessions/s-1/ask_user/respond", {"tool_call_id": "call_0", "response": "EUR", "gate_id": G1},
        ]]
    finally:
        ctx.close()


def test_the_panels_skip_names_the_gate_too() -> None:
    ctx = _panel(_PENDING)
    try:
        ctx.eval("MR.click('ask-user-skip');")
        assert _js(ctx, "CALLS") == [[
            "POST", "/sessions/s-1/yields/call_0/cancel", {"reason": "operator skipped", "gate_id": G1},
        ]]
    finally:
        ctx.close()


def test_a_panel_without_a_gate_id_sends_none() -> None:
    ctx = _panel({k: v for k, v in _PENDING.items() if k != "gate_id"})
    try:
        _answer(ctx)
        assert _js(ctx, "CALLS")[0][2] == {"tool_call_id": "call_0", "response": "EUR"}
    finally:
        ctx.close()


@pytest.mark.parametrize("act", ["answer", "skip"])
def test_a_stale_panel_says_so_inline_and_reloads(act) -> None:
    ctx = _panel(_PENDING)
    try:
        ctx.eval("NEXT.fail = " + json.dumps(STALE) + ";")
        if act == "answer":
            _answer(ctx)
        else:
            ctx.eval("MR.click('ask-user-skip');")
        ctx.eval("MR.rerender();")
        assert _js(ctx, "MR.find('ask-user-error').props.children") == "This question was replaced; the list is reloaded."
        assert _js(ctx, "REFETCH") == [1]
    finally:
        ctx.close()


# ---- the mobile Inbox's full call ------------------------------------------------------------------------------------------------------------------

_FULL_CALL_STUBS = r"""
var window = globalThis;
var ITEMS = [];
var SH_api = { sessionPendingYields: function () { return Promise.resolve({ items: ITEMS }); } };
var RESULT = null;
"""


def _full_call(item: dict, rows: list[dict]) -> dict:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        ctx.eval(_FULL_CALL_STUBS)
        start = MOBILE.index("function NV_inboxFullCall")
        ctx.eval(MOBILE[start:MOBILE.index("\n}\n", start) + 3])
        ctx.eval("ITEMS = " + json.dumps(rows) + "; NV_inboxFullCall(" + json.dumps(item) + ").then(function (r) { RESULT = r; });")
        return _js(ctx, "RESULT")
    finally:
        ctx.close()


def _yield_row(gate: str | None, args: dict) -> dict:
    row = {"tool_call_id": "call_0", "resume_metadata": {"original_call": {"id": "call_0", "name": "write", "arguments": args}}}
    if gate:
        row["gate_id"] = gate
    return row


def test_the_full_call_is_the_one_of_the_gate_the_item_names() -> None:
    item = {"workspace_id": "w1", "session_id": "s-1", "tool_call_id": "call_0", "gate_id": G2}
    got = _full_call(item, [_yield_row(G1, {"path": "old"}), _yield_row(G2, {"path": "new"})])
    assert json.loads(got["text"]) == {"path": "new"}


def test_the_full_call_of_a_gate_that_is_gone_is_gone_not_a_siblings() -> None:
    item = {"workspace_id": "w1", "session_id": "s-1", "tool_call_id": "call_0", "gate_id": G1}
    assert _full_call(item, [_yield_row(G2, {"path": "new"})]) == {"gone": True}


def test_an_item_without_a_gate_id_still_matches_by_the_raw_id() -> None:
    item = {"workspace_id": "w1", "session_id": "s-1", "tool_call_id": "call_0"}
    assert json.loads(_full_call(item, [_yield_row(None, {"path": "a"})])["text"]) == {"path": "a"}
