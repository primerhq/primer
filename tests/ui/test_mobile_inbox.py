"""The mobile Inbox describes what it asks you to decide (console review 2026-10-08, C-032 and C-033).

Before: an approval card showed only the session's name and "approval . primer", yet carried an Approve button that approved
whatever the session was parked on when it was tapped (it fetched the session's first pending yield AFTER the tap), with no toast.
The question and parked cards drew a border narrower than the card and put their button over the title.

The pure view and decision functions are evaluated in V8 against stubs of ``SH_api``; the layout and the whole flow run in a real
browser in ``tests/ui_e2e/test_mobile_inbox_journey.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")

_STUBS = r"""
var window = globalThis;
var CALLS = [];
var TOASTS = [];
var RESOLVED = 0;
var NEXT = { fail: null, yields: [] };
function toast(msg, extra) { TOASTS.push([msg, extra || null]); }
function onResolved() { RESOLVED += 1; }
var SH_api = {
  approve: function (sid, tcid) { CALLS.push(["approve", sid, tcid]); return NEXT.fail ? Promise.reject(NEXT.fail) : Promise.resolve({}); },
  reject: function (sid, tcid, reason) { CALLS.push(["reject", sid, tcid, reason]); return NEXT.fail ? Promise.reject(NEXT.fail) : Promise.resolve({}); },
  sessionPendingYields: function (wid, sid) {
    CALLS.push(["yields", wid, sid]);
    return NEXT.fail ? Promise.reject(NEXT.fail) : Promise.resolve({ items: NEXT.yields });
  },
};
var RESULT = null;
"""


@pytest.fixture
def inbox():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_STUBS)
    for name in ("NV_mobileInboxView", "NV_mobileInboxHeading", "NV_inboxDecide", "NV_inboxFullCall"):
        start = SHELL.index("function " + name)
        end = SHELL.index("\n}\n", start) + len("\n}\n")
        ctx.eval(SHELL[start:end])

    class Inbox:
        def set(self, **next_) -> None:
            ctx.eval("NEXT = Object.assign(NEXT, " + json.dumps(next_) + ");")

        def call(self, expression: str):
            return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))

        def run(self, expression: str) -> dict:
            ctx.eval("RESULT = null; " + expression + ".then(function (r) { RESULT = r; });")
            return {
                "result": json.loads(ctx.eval("JSON.stringify(RESULT)")),
                "calls": json.loads(ctx.eval("JSON.stringify(CALLS)")),
                "toasts": json.loads(ctx.eval("JSON.stringify(TOASTS)")),
                "resolved": ctx.eval("RESOLVED"),
            }

    try:
        yield Inbox()
    finally:
        ctx.close()


def _item(kind: str, **extra) -> dict:
    return {"workspace_id": "w1", "session_id": "sess-1", "session_name": None, "kind": kind, "tool_call_id": "tc-1", **extra}


_WRITE = {"tool_name": "workspaces__write_workspace_file", "arguments": "path=src/config/webhooks.ts, content=<3000 chars>", "truncated": True}


# --- what a card says ------------------------------------------------------------------------------------------------------


def test_an_approval_card_names_the_tool_and_its_arguments_and_can_be_decided_inline(inbox) -> None:
    view = inbox.call("NV_mobileInboxView(" + json.dumps(_item("approval", approval=_WRITE)) + ")")
    assert view["kindLabel"] == "Approval"
    assert view["line"] == "workspaces__write_workspace_file"
    assert view["args"] == "path=src/config/webhooks.ts, content=<3000 chars>" and view["truncated"] is True
    assert view["canApprove"] is True and view["canDeny"] is True


def test_an_approval_that_does_not_say_what_it_approves_gets_no_inline_decision(inbox) -> None:
    """Never blind: with nothing to show, the card says to open it. Nor is Deny offered: a card with no ``approval`` may not be a real
    ``_approval`` gate at all (a graph park whose primary gate is something else), and a Deny aimed at it would answer a misleading 404."""
    view = inbox.call("NV_mobileInboxView(" + json.dumps(_item("approval", approval=None)) + ")")
    assert view["canApprove"] is False and view["canDeny"] is False
    assert "approval" in view["line"].lower() and view["args"] == ""


def test_an_approval_with_no_call_id_cannot_be_decided_inline_at_all(inbox) -> None:
    """A decision names a call; without its id the card cannot say which call it decides."""
    view = inbox.call("NV_mobileInboxView(" + json.dumps(_item("approval", approval=_WRITE, tool_call_id=None)) + ")")
    assert view["canApprove"] is False and view["canDeny"] is False


def _who(username: str = "ana", role: str = "user") -> str:
    return json.dumps({"username": username, "role": role})


def test_an_inline_decision_is_hidden_from_someone_who_may_not_decide_it(inbox) -> None:
    """The route answers 403 approver_mismatch for a non-approver; the card does not offer a button that can only fail."""
    only_bob = {"kind": "users", "users": ["bob"], "roles": []}
    item = json.dumps(_item("approval", approval=_WRITE, approvers=only_bob))
    mine = inbox.call("NV_mobileInboxView(" + item + ", " + _who("ana") + ")")
    assert mine["canApprove"] is False and mine["canDeny"] is False and mine["notApprover"] is True
    assert "approver" in mine["note"].lower()
    bob = inbox.call("NV_mobileInboxView(" + item + ", " + _who("bob") + ")")
    assert bob["canApprove"] is True and bob["canDeny"] is True and bob["notApprover"] is False


def test_the_approver_rule_mirrors_the_servers(inbox) -> None:
    """ApproverSpec.allows: admins always; ``anyone`` and no spec admit everyone; ``roles`` by role; ``users`` by name."""
    def allowed(approvers, username="ana", role="user") -> bool:
        item = json.dumps(_item("approval", approval=_WRITE, approvers=approvers))
        return inbox.call("NV_mobileInboxView(" + item + ", " + _who(username, role) + ")")["canApprove"]

    assert allowed(None) and allowed({"kind": "anyone"})
    assert allowed({"kind": "users", "users": ["bob"]}, role="admin"), "an admin always passes"
    assert allowed({"kind": "roles", "roles": ["reviewer"]}, role="reviewer")
    assert not allowed({"kind": "roles", "roles": ["reviewer"]}, role="user")
    assert allowed({"kind": "users", "users": ["ana"]}) and not allowed({"kind": "users", "users": ["bob"]})
    assert allowed({"kind": "weird"}), "a malformed stored spec fails open, as the server does"


def test_a_question_card_shows_the_question(inbox) -> None:
    view = inbox.call("NV_mobileInboxView(" + json.dumps(_item("ask", prompt="Which environment should I deploy to?")) + ")")
    assert view["kindLabel"] == "Question" and view["line"] == "Which environment should I deploy to?"
    assert view["canApprove"] is False and view["canDeny"] is False


def test_a_question_without_a_prompt_and_a_parked_wait_say_something(inbox) -> None:
    ask = inbox.call("NV_mobileInboxView(" + json.dumps(_item("ask")) + ")")
    assert ask["line"], "an empty line would draw an empty card"
    parked = inbox.call("NV_mobileInboxView(" + json.dumps(_item("parked", prompt="README.md, docs/a.md")) + ")")
    assert parked["kindLabel"] == "Parked" and parked["line"] == "README.md, docs/a.md"
    assert inbox.call("NV_mobileInboxView(" + json.dumps(_item("parked")) + ")")["line"]


def test_the_heading_counts_what_is_waiting(inbox) -> None:
    assert inbox.call("NV_mobileInboxHeading(0)") == {"title": "Inbox", "count": "Nothing waiting on you"}
    assert inbox.call("NV_mobileInboxHeading(1)") == {"title": "Inbox", "count": "1 waiting on you"}
    assert inbox.call("NV_mobileInboxHeading(4)") == {"title": "Inbox", "count": "4 waiting on you"}


# --- deciding --------------------------------------------------------------------------------------------------------------


def test_approve_decides_exactly_the_call_the_card_showed_and_says_so(inbox) -> None:
    """It used to fetch the session's first pending yield AFTER the tap and approve that, whatever it had become."""
    got = inbox.run("NV_inboxDecide('approve', " + json.dumps(_item("approval", approval=_WRITE)) + ", toast, onResolved)")
    assert got["calls"] == [["approve", "sess-1", "tc-1"]], "one request, naming the card's own call; nothing is fetched first"
    assert got["result"] == {"ok": True}
    assert got["toasts"] == [["Approved workspaces__write_workspace_file", None]]
    assert got["resolved"] == 1


def test_deny_is_one_request_with_no_reason(inbox) -> None:
    got = inbox.run("NV_inboxDecide('deny', " + json.dumps(_item("approval", approval=_WRITE)) + ", toast, onResolved)")
    assert got["calls"] == [["reject", "sess-1", "tc-1", ""]]
    assert got["result"] == {"ok": True}
    assert got["toasts"] == [["Denied workspaces__write_workspace_file", None]]


def test_deciding_a_call_that_has_moved_on_says_so_and_refreshes_instead_of_deciding_something_else(inbox) -> None:
    inbox.set(fail={"status": 404, "detail": "No pending tool_approval with tool_call_id 'tc-1' on 'sess-1'"})
    got = inbox.run("NV_inboxDecide('approve', " + json.dumps(_item("approval", approval=_WRITE)) + ", toast, onResolved)")
    assert got["result"] == {"stale": True}
    assert len(got["toasts"]) == 1 and "moved on" in got["toasts"][0][0] and "review" in got["toasts"][0][0].lower()
    assert got["resolved"] == 1, "the stale card is refreshed away"
    assert [c[0] for c in got["calls"]] == ["approve"], "and no other call is approved in its place"


def test_a_409_is_treated_as_a_card_that_moved_on_too(inbox) -> None:
    """The respond route answers 404 for a gate that is no longer pending; a 409 (the session changed state under the call) means the
    same to the person holding the card, so it gets the same words and the same refresh."""
    inbox.set(fail={"status": 409, "detail": "Session 'sess-1' changed state"})
    got = inbox.run("NV_inboxDecide('deny', " + json.dumps(_item("approval", approval=_WRITE)) + ", toast, onResolved)")
    assert got["result"] == {"stale": True} and got["resolved"] == 1
    assert "moved on" in got["toasts"][0][0]


def test_any_other_failure_is_an_error_toast_with_the_reason_and_the_request_id(inbox) -> None:
    inbox.set(fail={"status": 500, "detail": "boom", "requestId": "req-7"})
    got = inbox.run("NV_inboxDecide('approve', " + json.dumps(_item("approval", approval=_WRITE)) + ", toast, onResolved)")
    assert got["result"] == {"failed": True}
    assert got["toasts"] == [["Approve failed: boom", {"kind": "error", "requestId": "req-7"}]]
    assert got["resolved"] == 0


def test_a_decision_without_a_call_id_sends_nothing(inbox) -> None:
    got = inbox.run("NV_inboxDecide('approve', " + json.dumps(_item("approval", approval=_WRITE, tool_call_id=None)) + ", toast, onResolved)")
    assert got["calls"] == [] and got["result"] == {"failed": True}
    assert got["toasts"] and got["toasts"][0][1] == {"kind": "error", "requestId": None}


# --- show all --------------------------------------------------------------------------------------------------------------


def test_show_all_loads_the_full_arguments_of_that_exact_call(inbox) -> None:
    other = {"tool_call_id": "tc-other", "resume_metadata": {"original_call": {"name": "bash", "arguments": {"command": "no"}}}}
    mine = {"tool_call_id": "tc-1", "resume_metadata": {"original_call": {
        "name": "workspaces__write_workspace_file", "arguments": {"path": "src/a.ts", "content": "hello"}}}}
    inbox.set(yields=[other, mine])
    got = inbox.run("NV_inboxFullCall(" + json.dumps(_item("approval", approval=_WRITE)) + ")")
    assert got["calls"] == [["yields", "w1", "sess-1"]]
    assert json.loads(got["result"]["text"]) == {"path": "src/a.ts", "content": "hello"}


def test_show_all_for_a_call_that_is_gone_says_so(inbox) -> None:
    inbox.set(yields=[{"tool_call_id": "tc-other", "resume_metadata": {}}])
    got = inbox.run("NV_inboxFullCall(" + json.dumps(_item("approval", approval=_WRITE)) + ")")
    assert got["result"] == {"gone": True}


def test_show_all_that_fails_reports_the_failure(inbox) -> None:
    inbox.set(fail={"status": 500, "detail": "boom"})
    got = inbox.run("NV_inboxFullCall(" + json.dumps(_item("approval", approval=_WRITE)) + ")")
    assert got["result"]["failed"] is True and "boom" in got["result"]["error"]
