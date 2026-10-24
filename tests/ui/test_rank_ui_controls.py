"""Human-input controls mirror the server's session-creator rank floor.

The server remains authoritative, but the console must not offer Approve,
Reject, Answer, Skip, or Cancel yield when it already knows the viewer is
outranked. Unknown data fails open so a loading or failed read does not invent
an authorization decision in the browser.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile
from tests.ui.test_gate_token_console_round2 import (
    _BANNER_STUBS,
    _PANEL_STUBS,
    _banner_source,
    _cancel_yield_btn_source,
    _panel_source,
    _transpile_source,
)

ROOT = Path(__file__).resolve().parents[2]
ATTENTION = (ROOT / "ui" / "foundation" / "shell-attention.js").read_text(encoding="utf-8")
DOC = ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx"
MOBILE = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
SESSION_DETAIL = (ROOT / "ui" / "components" / "session-detail.jsx").read_text(encoding="utf-8")
EXTERNAL = ROOT / "ui" / "components" / "external-tools.jsx"

ADMIN_SESSION = {
    "initiated_by": {
        "type": "user",
        "id": "user-admin",
        "display": "root",
        "role": "admin",
        "source": "local",
    }
}
USER_SESSION = {
    "initiated_by": {
        "type": "user",
        "id": "user-alice",
        "display": "alice",
        "role": "user",
        "source": "local",
    }
}
USER = {"type": "user", "id": "user-carol", "display": "carol", "role": "user", "source": "local"}
ADMIN = {"type": "user", "id": "user-admin-2", "display": "admin", "role": "admin", "source": "local"}


def _js(ctx, expression: str):
    return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))


@pytest.fixture
def attention():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(ATTENTION)
    try:
        yield ctx
    finally:
        ctx.close()


def test_the_rank_helper_matches_admin_user_own_unknown_and_token_cases(attention) -> None:
    cases = [
        (ADMIN, ADMIN_SESSION, True),
        (USER, ADMIN_SESSION, False),
        (USER, USER_SESSION, True),
        ({**USER, "id": "user-alice"}, ADMIN_SESSION, False),
        ({**USER, "id": "user-alice"}, {"initiated_by": {**ADMIN_SESSION["initiated_by"], "id": "user-alice"}}, True),
        ({"type": "api_token", "id": "at-1", "display": "build", "role": "restricted"},
         {"initiated_by": {"type": "api_token", "id": "at-1", "display": "build", "role": "admin", "source": "internal"}}, True),
        ({"type": "api_token", "id": "at-2", "display": "build", "role": "user"}, ADMIN_SESSION, False),
        ({"type": "system", "id": "system", "display": "system", "role": None}, ADMIN_SESSION, True),
        ({**USER, "role": None}, ADMIN_SESSION, True),
        (USER, None, True),
        (USER, {"initiated_by": None}, True),
        (USER, {"initiated_by": {"role": "admin"}}, True),
    ]
    for viewer, session, expected in cases:
        got = _js(attention, f"SH_mayDecideFor({json.dumps(viewer)}, {json.dumps(session)})")
        assert got is expected, (viewer, session, got)

    assert _js(attention, f"SH_rankDecisionWords({json.dumps(ADMIN_SESSION)})") == "Only an admin can decide this."
    assert _js(attention, f"SH_rankDecisionWords({json.dumps(USER_SESSION)})") == "Only a user or admin can decide this."


_CARD_PRELUDE = r"""
var window = globalThis;
var VIEWER = null;
function NV_useConsole() {
  return { username: "carol", role: "user", viewer: VIEWER, toast: function () {} };
}
function SH_looksLikeDiff() { return false; }
function SH_diffLineTone() { return "ctx"; }
var SH_api = {
  approve: function () { return Promise.resolve({}); },
  reject: function () { return Promise.resolve({}); },
  answer: function () { return Promise.resolve({}); },
};
"""


def _card(component: str, item: dict, viewer: dict, session: dict):
    ctx = mini_react_context(transpile(DOC), "var window = globalThis;\n" + ATTENTION + "\n" + _CARD_PRELUDE)
    ctx.eval("VIEWER = " + json.dumps(viewer) + ";")
    ctx.eval(
        f"MR.mount({component}, {{ item: {json.dumps(item)}, session: {json.dumps(session)}, ended: false, onResolved: function () {{}} }});"
    )
    return ctx


@pytest.mark.parametrize(
    ("component", "item", "buttons", "note"),
    [
        ("NV_DecisionCard", {"toolCallId": "tc-1", "sessionId": "s-1", "gatedTool": "write", "preview": ""},
         ("nv-approve", "nv-reject"), "nv-decision-rank-note"),
        ("NV_AskCard", {"toolCallId": "tc-1", "sessionId": "s-1", "title": "Continue?", "preview": "Continue?"},
         ("nv-ask-submit", "nv-ask-answer"), "nv-ask-rank-note"),
    ],
)
def test_desktop_cards_hide_human_input_when_the_viewer_is_outranked(component, item, buttons, note) -> None:
    ctx = _card(component, item, USER, ADMIN_SESSION)
    try:
        for testid in buttons:
            assert ctx.eval(f"MR.find({json.dumps(testid)}) === null") is True
        assert ctx.eval(f"MR.find({json.dumps(note)}).props.children") == "Only an admin can decide this."
    finally:
        ctx.close()


@pytest.mark.parametrize(
    ("component", "item", "button"),
    [
        ("NV_DecisionCard", {"toolCallId": "tc-1", "sessionId": "s-1", "gatedTool": "write", "preview": ""}, "nv-approve"),
        ("NV_AskCard", {"toolCallId": "tc-1", "sessionId": "s-1", "title": "Continue?", "preview": "Continue?"}, "nv-ask-submit"),
    ],
)
def test_desktop_cards_keep_human_input_at_the_same_rank(component, item, button) -> None:
    ctx = _card(component, item, USER, USER_SESSION)
    try:
        assert ctx.eval(f"MR.find({json.dumps(button)}) !== null") is True
    finally:
        ctx.close()


def _mobile_view_ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(ATTENTION)
    start = MOBILE.index("function NV_mobileMayDecide")
    end = MOBILE.index("\nfunction NV_mobileInboxHeading", start)
    ctx.eval(MOBILE[start:end])
    return ctx


def test_mobile_inbox_hides_approve_and_deny_at_the_rank_floor() -> None:
    row = {
        "kind": "approval",
        "tool_call_id": "tc-1",
        "approval": {"tool_name": "write", "arguments": "path=a"},
        **ADMIN_SESSION,
    }
    ctx = _mobile_view_ctx()
    try:
        blocked = _js(ctx, f"NV_mobileInboxView({json.dumps(row)}, {json.dumps(USER)}, false)")
        assert blocked["canApprove"] is False and blocked["canDeny"] is False
        assert blocked["rankBlocked"] is True
        assert blocked["note"] == "Only an admin can decide this."

        allowed = _js(ctx, f"NV_mobileInboxView({json.dumps({**row, **USER_SESSION})}, {json.dumps(USER)}, false)")
        assert allowed["canApprove"] is True and allowed["canDeny"] is True
        assert allowed["rankBlocked"] is False
    finally:
        ctx.close()


def test_mobile_inbox_decision_function_refuses_an_outranked_viewer_before_the_request() -> None:
    row = {
        "kind": "approval",
        "session_id": "s-1",
        "tool_call_id": "tc-1",
        "approval": {"tool_name": "write", "arguments": "path=a"},
        **ADMIN_SESSION,
    }
    ctx = _mobile_view_ctx()
    try:
        ctx.eval(
            "var CALLS = 0; var RESULT = null;"
            "var SH_api = { approve: function () { CALLS += 1; return Promise.resolve({}); },"
            " reject: function () { CALLS += 1; return Promise.resolve({}); } };"
            "function toast() {}"
        )
        ctx.eval(
            "NV_inboxDecide('approve', " + json.dumps(row)
            + ", toast, function () {}, " + json.dumps(USER)
            + ").then(function (value) { RESULT = value; });"
        )
        assert ctx.eval("CALLS") == 0
        assert _js(ctx, "RESULT") == {"blocked": True}
    finally:
        ctx.close()


def _banner(viewer: dict, session: dict):
    ctx = mini_react_context(_transpile_source(_banner_source(), "approval-banner.jsx"), _BANNER_STUBS + ATTENTION)
    data = {"tool_call_id": "tc-1", "tool_name": "write", "arguments": {}}
    ctx.eval(
        "MR.mount(ApprovalBanner, { data: " + json.dumps(data)
        + ", scope: 'sessions', id: 's-1', viewer: " + json.dumps(viewer)
        + ", session: " + json.dumps(session) + " });"
    )
    return ctx


def test_legacy_approval_banner_hides_decisions_at_the_rank_floor() -> None:
    ctx = _banner(USER, ADMIN_SESSION)
    try:
        assert ctx.eval("MR.find('approval-banner-approve') === null") is True
        assert ctx.eval("MR.find('approval-banner-reject') === null") is True
        assert ctx.eval("MR.find('approval-banner-rank-note').props.children") == "Only an admin can decide this."
    finally:
        ctx.close()

    ctx = _banner(USER, USER_SESSION)
    try:
        assert ctx.eval("MR.find('approval-banner-approve') !== null") is True
        assert ctx.eval("MR.find('approval-banner-reject') !== null") is True
    finally:
        ctx.close()


def _ask_panel(viewer: dict, session: dict):
    ctx = mini_react_context(_transpile_source(_panel_source(), "ask-user-panel.jsx"), _PANEL_STUBS + ATTENTION)
    pending = {"tool_call_id": "tc-1", "prompt": "Continue?", "response_schema": None}
    ctx.eval("PENDING.data = " + json.dumps(pending) + ";")
    ctx.eval(
        "MR.mount(AskUserPanel, { sid: 's-1', sessionStatus: 'waiting', pushToast: pushToast, viewer: "
        + json.dumps(viewer) + ", session: " + json.dumps(session) + " });"
    )
    return ctx


def test_legacy_question_panel_hides_answer_and_skip_at_the_rank_floor() -> None:
    ctx = _ask_panel(USER, ADMIN_SESSION)
    try:
        for testid in ("ask-user-input", "ask-user-submit", "ask-user-skip"):
            assert ctx.eval(f"MR.find({json.dumps(testid)}) === null") is True
        assert ctx.eval("MR.find('ask-user-rank-note').props.children") == "Only an admin can decide this."
    finally:
        ctx.close()

    ctx = _ask_panel(USER, USER_SESSION)
    try:
        assert ctx.eval("MR.find('ask-user-submit') !== null") is True
        assert ctx.eval("MR.find('ask-user-skip') !== null") is True
    finally:
        ctx.close()


_CANCEL_STUBS = r"""
var window = globalThis;
function Btn(p) { return React.createElement("button", { "data-testid": "cancel-yield" }, p.children); }
function _sdToastErr() { return function () {}; }
window.primerApi = {
  apiFetch: function () { return Promise.resolve({}); },
  useMutation: function () { return { loading: false, mutate: function () {} }; },
};
"""


def test_cancel_yield_hides_the_button_at_the_rank_floor() -> None:
    ctx = mini_react_context(_transpile_source(_cancel_yield_btn_source(), "cancel-yield-btn.jsx"), _CANCEL_STUBS + ATTENTION)
    try:
        ctx.eval(
            "MR.mount(CancelYieldBtn, { sid: 's-1', tcid: 'tc-1', toolName: 'sleep', viewer: "
            + json.dumps(USER) + ", session: " + json.dumps(ADMIN_SESSION) + " });"
        )
        assert ctx.eval("MR.find('cancel-yield') === null") is True
        assert ctx.eval("MR.find('yield-rank-note').props.children") == "Only an admin can decide this."
    finally:
        ctx.close()


_EXTERNAL_STUBS = r"""
var window = globalThis;
window.Btn = function (p) { return React.createElement("button", { "data-testid": "external-cancel" }, p.children); };
window.primerApi = {
  apiFetch: function () { return Promise.resolve({}); },
  useResource: function () { return { data: { items: [{ tool_call_id: "tc-1", tool_name: "client_tool", arguments: {} }] } }; },
};
"""


def test_external_wait_hides_cancel_at_the_rank_floor() -> None:
    ctx = mini_react_context(transpile(EXTERNAL), _EXTERNAL_STUBS + ATTENTION)
    try:
        ctx.eval(
            "MR.mount(window.ExternalPendingBanner, { sessionId: 's-1', viewer: "
            + json.dumps(USER) + ", session: " + json.dumps(ADMIN_SESSION) + " });"
        )
        assert ctx.eval("MR.find('external-cancel') === null") is True
        assert ctx.eval("MR.find('external-rank-note').props.children") == "Only an admin can decide this."
    finally:
        ctx.close()


def test_shell_context_carries_the_principal_returned_by_auth_status() -> None:
    assert "viewer: status.data && status.data.principal" in SHELL


def test_legacy_wrappers_pass_the_viewer_and_session_to_the_controls() -> None:
    assert "viewer={viewer} session={session}" in SESSION_DETAIL
