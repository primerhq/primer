"""The approval card's reject form says what its button now does and can be folded again (console review C-018, 2026-10-08).

"Reject with feedback" opened a reason box on the first click and was ALSO the confirm: the red button read the same before and after, there was
no hint that a second click sends, and no way to fold the box without sending or leaving the card. The first click still opens the box (the
resting label is unchanged); once open the button reads "Send rejection" (still disabled until there is a reason) and a Cancel folds the form
without a request. Approve is never affected by an open form.

The real ``NV_DecisionCard`` runs in V8 on the hook runtime in ``tests/ui/_mini_react.py``; ``SH_api`` records what would have been sent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx"

_PRELUDE = r"""
var TOASTS = [];
var REJECTS = [];
var APPROVES = [];
var RESOLVED = [];
function NV_useConsole() { return { username: "reviewer", role: "admin", toast: function (m) { TOASTS.push(m); } }; }
function SH_routingLine() { return "anyone may decide"; }
function SH_looksLikeDiff() { return false; }
function SH_diffLineTone() { return "ctx"; }
var SH_api = {
  approve: function (sid, tc) { APPROVES.push([sid, tc]); return Promise.resolve({}); },
  reject: function (sid, tc, reason) { REJECTS.push([sid, tc, reason]); return Promise.resolve({}); },
};
function __props(testid) {
  var el = MR.find(testid);
  if (!el) return null;
  var p = el.props;
  return {
    label: typeof p.children === "string" ? p.children : null, disabled: !!p.disabled, ariaLabel: p["aria-label"],
    placeholder: p.placeholder, value: p.value, autoFocus: !!p.autoFocus, type: p.type,
  };
}
function __type(value) { MR.find("nv-reject-reason").props.onChange({ target: { value: value } }); MR.rerender(); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(transpile(DOC), _PRELUDE)
    c.eval(
        "MR.mount(NV_DecisionCard, { item: { toolCallId: 'tc-1', sessionId: 's-1', gatedTool: 'write', preview: '' },"
        " ended: false, live: true, onResolved: function () { RESOLVED.push(1); } });"
    )
    try:
        yield c
    finally:
        c.close()


def _props(ctx, testid: str) -> dict | None:
    return json.loads(ctx.eval(f"JSON.stringify(__props({json.dumps(testid)}))"))


def _calls(ctx, name: str) -> list:
    return json.loads(ctx.eval(f"JSON.stringify({name})"))


def test_at_rest_the_button_reads_reject_with_feedback_and_no_form_is_shown(ctx) -> None:
    button = _props(ctx, "nv-reject")
    assert button["label"] == "Reject with feedback" and not button["disabled"]
    assert _props(ctx, "nv-reject-reason") is None
    assert _props(ctx, "nv-reject-cancel") is None


def test_the_first_click_opens_the_form_and_the_button_now_says_it_sends(ctx) -> None:
    ctx.eval("MR.click('nv-reject');")
    button = _props(ctx, "nv-reject")
    assert button["label"] == "Send rejection"
    assert button["disabled"], "nothing to send until there is a reason"
    assert _calls(ctx, "REJECTS") == [], "the opening click sends nothing"
    cancel = _props(ctx, "nv-reject-cancel")
    assert cancel is not None and cancel["label"] == "Cancel" and cancel["type"] == "button"
    box = _props(ctx, "nv-reject-reason")
    assert box["ariaLabel"], "the box is named for a screen reader"
    assert box["autoFocus"], "the box takes the focus the click moved away from the button"


def test_a_reason_enables_send_and_send_posts_it_once(ctx) -> None:
    ctx.eval("MR.click('nv-reject'); __type('denied by security review');")
    assert not _props(ctx, "nv-reject")["disabled"]
    ctx.eval("MR.click('nv-reject');")
    assert _calls(ctx, "REJECTS") == [["s-1", "tc-1", "denied by security review"]]


def test_whitespace_alone_is_not_a_reason(ctx) -> None:
    ctx.eval("MR.click('nv-reject'); __type('   ');")
    assert _props(ctx, "nv-reject")["disabled"]


def test_cancel_folds_the_form_without_a_request_and_restores_the_resting_label(ctx) -> None:
    ctx.eval("MR.click('nv-reject'); __type('half a thought'); MR.click('nv-reject-cancel');")
    assert _props(ctx, "nv-reject-reason") is None and _props(ctx, "nv-reject-cancel") is None
    button = _props(ctx, "nv-reject")
    assert button["label"] == "Reject with feedback" and not button["disabled"]
    assert _calls(ctx, "REJECTS") == [] and _calls(ctx, "RESOLVED") == []


def test_a_cancelled_draft_is_not_sent_by_the_next_open(ctx) -> None:
    """Cancel drops the draft: reopening starts empty, so the first click after a fold opens the box and cannot send the old text."""
    ctx.eval("MR.click('nv-reject'); __type('half a thought'); MR.click('nv-reject-cancel'); MR.click('nv-reject');")
    assert _props(ctx, "nv-reject-reason")["value"] == ""
    assert _props(ctx, "nv-reject")["disabled"]
    assert _calls(ctx, "REJECTS") == []


def test_approve_is_unaffected_by_an_open_form(ctx) -> None:
    ctx.eval("MR.click('nv-reject'); __type('no');")
    assert not _props(ctx, "nv-approve")["disabled"]
    ctx.eval("MR.click('nv-approve');")
    assert _calls(ctx, "APPROVES") == [["s-1", "tc-1"]]
    assert _calls(ctx, "REJECTS") == []


def test_an_ended_session_keeps_the_form_read_only(ctx) -> None:
    ctx.eval("MR.click('nv-reject'); __type('too late');")
    ctx.eval(
        "MR.rerender({ item: { toolCallId: 'tc-1', sessionId: 's-1', gatedTool: 'write', preview: '' }, ended: true, live: false,"
        " onResolved: function () {} });"
    )
    assert _props(ctx, "nv-reject")["disabled"]
    assert _props(ctx, "nv-reject-reason") is not None, "folding is the user's choice, the card does not collapse on its own"
