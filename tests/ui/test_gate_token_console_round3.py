"""The console, round 3 of the C-033 review: a draft, an expanded card and a reject reason belong to ONE gate (console review C-033, ticket 01a11f52-9d98).

The provider repeats its ``tool_call_id`` across rounds, so a surface keyed or reset by the raw id keeps its local state when the question or the approval
is REPLACED under the same id (the 409 reload, the poll). Each of the three below carried state across that replacement and then sent it with the NEW gate's id:

* the legacy ``AskUserPanel`` cleared its draft on a change of ``tool_call_id`` only;
* the mobile Inbox card was keyed by session, so its "Show all" (the full arguments of round 1) stayed open under round 3's summary;
* the legacy ``ApprovalBanner`` kept the half-typed reject reason of the gate it was drawn for.

A refresh of the SAME gate keeps all three (the pending lists are polled).
"""

from __future__ import annotations

import json
import re

from tests.ui._mini_react import mini_react_context
from tests.ui.test_gate_token_console import ATTENTION, STALE, _js
from tests.ui.test_gate_token_console_round2 import (
    _BANNER_STUBS,
    _PENDING,
    APPROVALS,
    G1,
    G2,
    MOBILE,
    SESSION_DETAIL,
    _panel,
    _transpile_source,
)

# ---- the legacy ask_user panel ---------------------------------------------------------------------------------------------------------------------


def _typed(ctx) -> str:
    return ctx.eval("MR.find('ask-user-input').props.value")


def test_a_replaced_question_does_not_inherit_the_draft() -> None:
    ctx = _panel(_PENDING)
    try:
        ctx.eval("MR.find('ask-user-input').props.onChange({ target: { value: 'EUR' } }); MR.rerender();")
        ctx.eval("NEXT.fail = " + json.dumps(STALE) + "; MR.click('ask-user-submit');")
        ctx.eval("MR.rerender();")
        # the reload brings round 3's question under the same raw id, with its own gate id
        ctx.eval("NEXT.fail = null; PENDING.data = " + json.dumps({**_PENDING, "prompt": "Delete which workspace?", "gate_id": G2}) + "; MR.rerender();")

        assert _typed(ctx) == "", "the answer typed for round 1's question is still in the box for round 3's"
        ctx.eval("MR.find('ask-user-input').props.onChange({ target: { value: 'workspace-b' } }); MR.rerender(); MR.click('ask-user-submit');")
        sent = _js(ctx, "CALLS")[-1][2]
        assert sent.get("gate_id") == G2 and sent.get("response") == "workspace-b", sent
    finally:
        ctx.close()


def test_the_draft_survives_a_poll_of_the_same_question() -> None:
    ctx = _panel(_PENDING)
    try:
        ctx.eval("MR.find('ask-user-input').props.onChange({ target: { value: 'EUR' } }); MR.rerender();")
        ctx.eval("PENDING.data = " + json.dumps({**_PENDING}) + "; MR.rerender();")

        assert _typed(ctx) == "EUR"
    finally:
        ctx.close()


def test_a_question_from_before_gates_had_ids_keeps_its_draft_on_a_poll() -> None:
    """A row from before gates had ids has no gate id: the raw id still decides, as it did."""
    old = {k: v for k, v in _PENDING.items() if k != "gate_id"}
    ctx = _panel(old)
    try:
        ctx.eval("MR.find('ask-user-input').props.onChange({ target: { value: 'EUR' } }); MR.rerender();")
        ctx.eval("PENDING.data = " + json.dumps({**old}) + "; MR.rerender();")

        assert _typed(ctx) == "EUR"
    finally:
        ctx.close()


# ---- the mobile Inbox card ---------------------------------------------------------------------------------------------------------------------------

G3 = "c" * 32
_MOBILE_STUBS = r"""
var window = globalThis;
var YIELDS = [];
function NV_useConsole() { return { username: "admin", role: "admin", toast: function () {}, setDoc: function () {} }; }
function NV_identity() { return { color: "red", d: "" }; }
function NV_errText(e) { return e ? String(e) : null; }
var SH_api = {
  sessionPendingYields: function () { return Promise.resolve({ items: YIELDS }); },
  approve: function () { return Promise.resolve({}); },
  reject: function () { return Promise.resolve({}); },
};
"""

_MOBILE_FUNCTIONS = (
    "NV_mobileMayDecide", "NV_mobileInboxView", "NV_mobileInboxHeading", "NV_inboxDecide", "NV_inboxFullCall", "NV_inboxFullFor",
    "NV_MobileDecisionButton", "NV_MobileInboxCard", "NV_MobileInboxPanel",
)


def _mobile_fn(name: str) -> str:
    start = MOBILE.index("function " + name + "(")
    return MOBILE[start:MOBILE.index("\n}\n", start) + 3]


def _item(gate: str, args: str) -> dict:
    return {
        "workspace_id": "w1", "session_id": "sess-1", "kind": "approval", "tool_call_id": "call_0", "gate_id": gate, "approvers": None,
        "approval": {"tool_name": "workspaces__write_workspace_file", "arguments": args, "truncated": True},
    }


def _row(gate: str, path: str) -> dict:
    return {
        "tool_call_id": "call_0", "gate_id": gate,
        "resume_metadata": {"original_call": {"id": "call_0", "name": "write", "arguments": {"path": path}}},
    }


def _mobile_ctx():
    source = _transpile_source("".join(_mobile_fn(n) for n in _MOBILE_FUNCTIONS), "mobile-inbox.jsx")
    return mini_react_context(source, _MOBILE_STUBS + ATTENTION)


def _props(item: dict) -> str:
    return "{ items: [" + json.dumps(item) + "], loaded: true, onResolved: function () {} }"


def _open_the_full_call(ctx, gate: str, path: str) -> None:
    ctx.eval("YIELDS = " + json.dumps([_row(gate, path)]) + ";")
    ctx.eval("MR.mount(NV_MobileInboxPanel, " + _props(_item(gate, "path=" + path + ", ...")) + ");")
    ctx.eval("MR.click('nv-mob-ib-showall:sess-1');")
    ctx.eval("MR.rerender();")


def _texts(ctx) -> list[str]:
    return json.loads(ctx.eval("JSON.stringify(MR.texts())"))


def test_a_replaced_gate_does_not_keep_the_old_full_call_open() -> None:
    ctx = _mobile_ctx()
    try:
        _open_the_full_call(ctx, G1, "src/OLD-round1.ts")
        assert any("OLD-round1" in t for t in _texts(ctx)), "the setup did not expand the full call"

        # the Inbox reloads: the same session is now parked on round 3's gate under the same raw id
        ctx.eval("YIELDS = " + json.dumps([_row(G3, "src/NEW-round3.ts")]) + ";")
        ctx.eval("MR.rerender(" + _props(_item(G3, "path=src/NEW-round3.ts, ...")) + ");")

        assert not any("OLD-round1" in t for t in _texts(ctx)), "round 1's full call is still shown on the card of round 3's gate"
    finally:
        ctx.close()


def test_the_full_call_stays_open_across_a_poll_of_the_same_gate() -> None:
    ctx = _mobile_ctx()
    try:
        _open_the_full_call(ctx, G1, "src/OLD-round1.ts")

        ctx.eval("MR.rerender(" + _props(_item(G1, "path=src/OLD-round1.ts, ...")) + ");")

        assert any("OLD-round1" in t for t in _texts(ctx))
    finally:
        ctx.close()


def test_the_mobile_card_is_keyed_by_the_gate() -> None:
    assert re.search(r"<NV_MobileInboxCard\s+key=\{SH_pendingId\(it\.session_id,\s*it\.gate_id,\s*it\.tool_call_id\)\}", MOBILE)


# ---- the legacy approval banner ------------------------------------------------------------------------------------------------------------------------

_PANEL_BANNER_STUBS = _BANNER_STUBS + r"""
var SESSION_TERMINAL = new Set();
var PENDING = { data: null, error: null };
window.SessionCountdown = function () { return null; };
window.primerApi.useResource = function () { return { data: PENDING.data, error: PENDING.error, refetch: function () {} }; };
"""


def _banner_panel_source() -> str:
    start = APPROVALS.index("function AP_toastErr")
    toast = APPROVALS[start:APPROVALS.index("\n}\n", start) + 3]
    start = APPROVALS.index("function ApprovalBanner")
    banner = APPROVALS[start:APPROVALS.index("\n}\n", start) + 3]
    start = SESSION_DETAIL.index("function ApprovalBannerPanel")
    panel = SESSION_DETAIL[start:SESSION_DETAIL.index("\n}\n", start) + 3]
    return toast + banner + panel


_BANNER = {"tool_call_id": "call_0", "tool_name": "write", "arguments": {}, "gate_id": G1}


def _banner_panel():
    ctx = mini_react_context(_transpile_source(_banner_panel_source(), "approval-banner-panel.jsx"), _PANEL_BANNER_STUBS + ATTENTION)
    ctx.eval("PENDING.data = " + json.dumps(_BANNER) + ";")
    ctx.eval("MR.mount(ApprovalBannerPanel, { sid: 's-1', sessionStatus: 'waiting', session: null, pushToast: pushToast });")
    return ctx


def _type_a_rejection(ctx) -> None:
    ctx.eval("MR.click('approval-banner-reject');")
    ctx.eval("MR.find('approval-banner-reason').props.onChange({ target: { value: 'no, wrong path' } }); MR.rerender();")


def test_a_replaced_approval_does_not_inherit_the_half_typed_rejection() -> None:
    ctx = _banner_panel()
    try:
        _type_a_rejection(ctx)
        assert ctx.eval("MR.find('approval-banner-reason').props.value") == "no, wrong path"

        ctx.eval("PENDING.data = " + json.dumps({**_BANNER, "gate_id": G2, "tool_name": "delete_workspace"}) + "; MR.rerender();")

        assert ctx.eval("MR.find('approval-banner-reason') === null") is True, "round 3's approval opened with round 1's rejection reason typed in"
    finally:
        ctx.close()


def test_the_rejection_survives_a_poll_of_the_same_approval() -> None:
    ctx = _banner_panel()
    try:
        _type_a_rejection(ctx)

        ctx.eval("PENDING.data = " + json.dumps({**_BANNER}) + "; MR.rerender();")

        assert ctx.eval("MR.find('approval-banner-reason').props.value") == "no, wrong path"
    finally:
        ctx.close()


def test_the_banner_is_keyed_by_the_gate() -> None:
    start = SESSION_DETAIL.index("function ApprovalBannerPanel")
    body = SESSION_DETAIL[start:SESSION_DETAIL.index("\n}\n", start)]
    assert re.search(r"<ApprovalBanner\s+key=\{", body) and "SH_pendingId" in body
