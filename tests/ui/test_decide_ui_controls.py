"""Human-input controls mirror the server's gate on a person's input into a session (access-control P2).

Answering a question, deciding an approval and cancelling a yield need full access on the session's workspace on the server
(``require_session`` FULL, with an approval's approver spec on top); the session's creator and its rank no longer decide. The
console must not offer Approve, Reject, Answer, Skip or Cancel yield when it already knows the viewer holds only read, and an
unknown level fails open so a loading or failed read does not invent an authorization decision in the browser.
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


WORDS = "You need full access to this workspace to decide this."
SYSTEM = {"type": "system", "id": "system", "display": "system", "role": None, "source": "internal"}


def test_the_helper_decides_by_the_workspace_level_not_the_creator(attention) -> None:
    cases = [
        (USER, ADMIN_SESSION, "full", True),   # an admin started it: the creator no longer outranks a full-access peer
        (USER, USER_SESSION, "owner", True),
        (USER, USER_SESSION, "read", False),
        (USER, USER_SESSION, None, False),     # no level at all
        (USER, ADMIN_SESSION, "__unknown__", True),  # the level has not loaded: fail open
        (ADMIN, USER_SESSION, "read", True),   # admins always may
        (SYSTEM, USER_SESSION, None, True),
        (None, USER_SESSION, "read", True),    # no viewer: fail open
    ]
    for viewer, session, level, expected in cases:
        arg = "undefined" if level == "__unknown__" else json.dumps(level)
        got = _js(attention, f"SH_mayDecideFor({json.dumps(viewer)}, {json.dumps(session)}, {arg})")
        assert got is expected, (viewer, session, level)
    assert _js(attention, f"SH_mayDecideFor({json.dumps(USER)}, null, 'read')") is True
    assert _js(attention, "SH_accessDecisionWords()") == WORDS


def test_the_level_of_a_workspace_row_mirrors_level_of(attention) -> None:
    def level(viewer, access):
        row = {"id": "ws-1"} if access is None else {"id": "ws-1", "access": access}
        return _js(attention, f"SH_workspaceLevel({json.dumps(viewer)}, {json.dumps(row)}) || 'none'")

    carol = {**USER, "user_id": "user-carol"}
    assert level(carol, {"owners": ["user-carol"], "grants": {}}) == "owner"
    assert level(carol, {"owners": ["u-x"], "grants": {"user-carol": "full"}}) == "full"
    assert level(carol, {"owners": ["u-x"], "grants": {"user-carol": "read"}}) == "read"
    assert level(carol, {"owners": ["u-x"], "grants": {}, "public": True}) == "read"
    assert level({**carol, "role": "restricted"}, {"owners": ["u-x"], "grants": {}, "public": True}) == "none"
    assert level(carol, {"owners": ["u-x"], "grants": {}}) == "none"
    assert level(ADMIN, {"owners": ["u-x"], "grants": {}}) == "owner"
    assert _js(attention, f"SH_workspaceLevel({json.dumps(carol)}, {{'id': 'ws-1'}}) === undefined") is True


def test_the_helper_no_longer_reads_the_creator_or_a_rank() -> None:
    start = ATTENTION.index("function SH_mayDecideFor")
    body = ATTENTION[start:ATTENTION.index("\n}\n", start)]
    assert "initiated_by" not in body and "RANK" not in body
    assert "SH_ROLE_RANK" not in ATTENTION and "SH_rankDecisionWords" not in ATTENTION
    doc = DOC.read_text(encoding="utf-8")
    assert doc.count("SH_mayDecideFor(con.viewer, props.session, con.wsLevel || undefined)") == 2
    assert "SH_mayDecideFor(who, it, level)" in MOBILE and "SH_mayDecideFor(viewer, it, level)" in MOBILE


_CARD_PRELUDE = r"""
var window = globalThis;
var VIEWER = null;
var LEVEL = null;
function NV_useConsole() {
  return { username: "carol", role: "user", viewer: VIEWER, wsLevel: LEVEL, toast: function () {} };
}
function SH_looksLikeDiff() { return false; }
function SH_diffLineTone() { return "ctx"; }
var SH_api = {
  approve: function () { return Promise.resolve({}); },
  reject: function () { return Promise.resolve({}); },
  answer: function () { return Promise.resolve({}); },
};
"""


def _card(component: str, item: dict, viewer: dict, session: dict, level: str | None):
    ctx = mini_react_context(transpile(DOC), "var window = globalThis;\n" + ATTENTION + "\n" + _CARD_PRELUDE)
    ctx.eval("VIEWER = " + json.dumps(viewer) + "; LEVEL = " + json.dumps(level) + ";")
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
def test_desktop_cards_hide_human_input_from_a_reader(component, item, buttons, note) -> None:
    ctx = _card(component, item, USER, USER_SESSION, "read")
    try:
        for testid in buttons:
            assert ctx.eval(f"MR.find({json.dumps(testid)}) === null") is True
        assert ctx.eval(f"MR.find({json.dumps(note)}).props.children") == WORDS
    finally:
        ctx.close()


@pytest.mark.parametrize(
    ("component", "item", "button"),
    [
        ("NV_DecisionCard", {"toolCallId": "tc-1", "sessionId": "s-1", "gatedTool": "write", "preview": ""}, "nv-approve"),
        ("NV_AskCard", {"toolCallId": "tc-1", "sessionId": "s-1", "title": "Continue?", "preview": "Continue?"}, "nv-ask-submit"),
    ],
)
def test_desktop_cards_keep_human_input_at_full_whoever_started_it(component, item, button) -> None:
    ctx = _card(component, item, USER, ADMIN_SESSION, "full")
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
    decide_start = MOBILE.index("function NV_inboxDecide")
    decide_end = MOBILE.index("\n}\n", decide_start) + len("\n}\n")
    ctx.eval(MOBILE[decide_start:decide_end])
    return ctx


def test_mobile_inbox_hides_approve_and_deny_from_a_reader() -> None:
    row = {
        "kind": "approval",
        "tool_call_id": "tc-1",
        "approval": {"tool_name": "write", "arguments": "path=a"},
        **ADMIN_SESSION,
    }
    ctx = _mobile_view_ctx()
    try:
        blocked = _js(ctx, f"NV_mobileInboxView({json.dumps({**row, **USER_SESSION})}, {json.dumps(USER)}, false, 'read')")
        assert blocked["canApprove"] is False and blocked["canDeny"] is False
        assert blocked["rankBlocked"] is True
        assert blocked["note"] == WORDS

        allowed = _js(ctx, f"NV_mobileInboxView({json.dumps(row)}, {json.dumps(USER)}, false, 'full')")
        assert allowed["canApprove"] is True and allowed["canDeny"] is True
        assert allowed["rankBlocked"] is False
    finally:
        ctx.close()


def test_mobile_inbox_decision_function_refuses_a_reader_before_the_request() -> None:
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
            + ", 'read').then(function (value) { RESULT = value; });"
        )
        assert ctx.eval("CALLS") == 0
        assert _js(ctx, "RESULT") == {"blocked": True}
    finally:
        ctx.close()


def _banner(viewer: dict, session: dict, level: str | None):
    ctx = mini_react_context(_transpile_source(_banner_source(), "approval-banner.jsx"), _BANNER_STUBS + ATTENTION)
    data = {"tool_call_id": "tc-1", "tool_name": "write", "arguments": {}}
    ctx.eval(
        "MR.mount(ApprovalBanner, { data: " + json.dumps(data)
        + ", scope: 'sessions', id: 's-1', viewer: " + json.dumps(viewer)
        + ", session: " + json.dumps(session) + ", level: " + json.dumps(level) + " });"
    )
    return ctx


def test_legacy_approval_banner_hides_decisions_from_a_reader() -> None:
    ctx = _banner(USER, USER_SESSION, "read")
    try:
        assert ctx.eval("MR.find('approval-banner-approve') === null") is True
        assert ctx.eval("MR.find('approval-banner-reject') === null") is True
        assert ctx.eval("MR.find('approval-banner-rank-note').props.children") == WORDS
    finally:
        ctx.close()

    ctx = _banner(USER, ADMIN_SESSION, "full")
    try:
        assert ctx.eval("MR.find('approval-banner-approve') !== null") is True
        assert ctx.eval("MR.find('approval-banner-reject') !== null") is True
    finally:
        ctx.close()


def _ask_panel(viewer: dict, session: dict, level: str | None):
    ctx = mini_react_context(_transpile_source(_panel_source(), "ask-user-panel.jsx"), _PANEL_STUBS + ATTENTION)
    pending = {"tool_call_id": "tc-1", "prompt": "Continue?", "response_schema": None}
    ctx.eval("PENDING.data = " + json.dumps(pending) + ";")
    ctx.eval(
        "MR.mount(AskUserPanel, { sid: 's-1', sessionStatus: 'waiting', pushToast: pushToast, viewer: "
        + json.dumps(viewer) + ", session: " + json.dumps(session) + ", level: " + json.dumps(level) + " });"
    )
    return ctx


def test_legacy_question_panel_hides_answer_and_skip_from_a_reader() -> None:
    ctx = _ask_panel(USER, USER_SESSION, "read")
    try:
        for testid in ("ask-user-input", "ask-user-submit", "ask-user-skip"):
            assert ctx.eval(f"MR.find({json.dumps(testid)}) === null") is True
        assert ctx.eval("MR.find('ask-user-rank-note').props.children") == WORDS
    finally:
        ctx.close()

    ctx = _ask_panel(USER, ADMIN_SESSION, "full")
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


def test_cancel_yield_hides_the_button_from_a_reader() -> None:
    ctx = mini_react_context(_transpile_source(_cancel_yield_btn_source(), "cancel-yield-btn.jsx"), _CANCEL_STUBS + ATTENTION)
    try:
        ctx.eval(
            "MR.mount(CancelYieldBtn, { sid: 's-1', tcid: 'tc-1', toolName: 'sleep', viewer: "
            + json.dumps(USER) + ", session: " + json.dumps(USER_SESSION) + ", level: 'read' });"
        )
        assert ctx.eval("MR.find('cancel-yield') === null") is True
        assert ctx.eval("MR.find('yield-rank-note').props.children") == WORDS
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


def test_external_wait_hides_cancel_from_a_reader() -> None:
    ctx = mini_react_context(transpile(EXTERNAL), _EXTERNAL_STUBS + ATTENTION)
    try:
        ctx.eval(
            "MR.mount(window.ExternalPendingBanner, { sessionId: 's-1', viewer: "
            + json.dumps(USER) + ", session: " + json.dumps(USER_SESSION) + ", level: 'read' });"
        )
        assert ctx.eval("MR.find('external-cancel') === null") is True
        assert ctx.eval("MR.find('external-rank-note').props.children") == WORDS
    finally:
        ctx.close()


def test_shell_context_carries_the_principal_returned_by_auth_status() -> None:
    assert "viewer: status.data && status.data.principal" in SHELL


def test_legacy_wrappers_pass_the_viewer_and_session_to_the_controls() -> None:
    assert "viewer={viewer} session={session} level={level}" in SESSION_DETAIL
    assert "session={session} viewer={con.viewer} level={con.wsLevel || undefined}" in DOC.read_text(encoding="utf-8")
