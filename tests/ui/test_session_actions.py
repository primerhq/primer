"""End, Park and Delete of a session say what happened (console review 2026-10-08, C-012, C-022, C-034).

Before: End and Park were reachable only by right-clicking a rail row, which a phone cannot do and the palette did not offer; the
overflow's "Close Session" ended the session without asking; and End, Park and Delete had no failure path at all, so deleting a
running session (the server answers 409) showed nothing and the turn kept running. The three helpers below back the rail menu, the
session header's overflow menu (the phone's only menu) and the palette verbs, so each of them confirms where it should and every
outcome reaches the toast.

The helpers are evaluated in V8 against stubs of ``SH_api`` and the confirm dialog; the menus that call them are driven in a real
browser by ``tests/ui_e2e/test_session_actions_journey.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
RAIL = (ROOT / "ui" / "components" / "console" / "nv-rail.jsx").read_text(encoding="utf-8")
VERBS = (ROOT / "ui" / "foundation" / "shell-verbs.js").read_text(encoding="utf-8")

_STUBS = r"""
var window = globalThis;
var CALLS = [];
var TOASTS = [];
var REFETCHES = 0;
var DELETED = 0;
var NEXT = { confirm: true, fail: null };
var RESULT = null;
function refetch() { REFETCHES += 1; }
function onDeleted() { DELETED += 1; }
function toast(msg, extra) { TOASTS.push([msg, extra || null]); }
function outcome(name, wid, sid) {
  CALLS.push([name, wid, sid]);
  if (NEXT.fail) return Promise.reject(NEXT.fail);
  return Promise.resolve(name === "cancel" ? { id: sid, status: "ended" } : null);
}
var SH_api = {
  pause: function (wid, sid) { return outcome("pause", wid, sid); },
  cancel: function (wid, sid) { return outcome("cancel", wid, sid); },
  deleteSession: function (wid, sid) { return outcome("delete", wid, sid); },
};
window.confirmDialog = function (opts) { CALLS.push(["confirm", opts]); return Promise.resolve(NEXT.confirm); };
"""


@pytest.fixture
def actions():
    """A V8 context with the three helpers loaded; ``run`` calls one and returns what it resolved to and what it did."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_STUBS)
    for name in ("NV_failToast", "NV_doPark", "NV_doEnd", "NV_doDelete"):
        start = DOC.index("function " + name)
        end = DOC.index("\n}\n", start) + len("\n}\n")
        ctx.eval(DOC[start:end])

    class Actions:
        def set(self, *, confirm: bool = True, fail: dict | None = None) -> None:
            ctx.eval("NEXT = " + json.dumps({"confirm": confirm, "fail": fail}) + ";")

        def run(self, call: str) -> dict:
            ctx.eval("RESULT = null; " + call + ".then(function (r) { RESULT = r; });")
            return {
                "result": json.loads(ctx.eval("JSON.stringify(RESULT)")),
                "calls": json.loads(ctx.eval("JSON.stringify(CALLS)")),
                "toasts": json.loads(ctx.eval("JSON.stringify(TOASTS)")),
                "refetches": ctx.eval("REFETCHES"),
                "deleted": ctx.eval("DELETED"),
            }

    try:
        yield Actions()
    finally:
        ctx.close()


_CONFLICT = {"status": 409, "detail": "Session 'sess-1' is running; end it before deleting it", "requestId": "req-409"}


# --- Park -----------------------------------------------------------------------------------------------------------------


def test_park_pauses_the_session_and_says_how_to_get_it_back(actions) -> None:
    got = actions.run('NV_doPark("w1", "sess-1", refetch, toast)')
    assert got["calls"] == [["pause", "w1", "sess-1"]], "Park is the pause route and asks nothing first: a message resumes it"
    assert got["result"] == {"ok": True}
    assert len(got["toasts"]) == 1 and "paused" in got["toasts"][0][0].lower() and "message" in got["toasts"][0][0].lower()
    assert got["toasts"][0][1] is None, "success is not an error toast"
    assert got["refetches"] == 1


def test_parking_a_running_session_says_it_pauses_when_the_turn_ends(actions) -> None:
    """For a running session the pause route only sets a flag the worker honours at the next turn boundary, so 'paused' would be a lie."""
    got = actions.run('NV_doPark("w1", "sess-1", refetch, toast, true)')
    assert got["result"] == {"ok": True}
    text = got["toasts"][0][0].lower()
    assert "requested" in text and "turn" in text and "paused." not in text, got["toasts"]


def test_park_that_fails_says_so_with_the_servers_reason_and_the_request_id(actions) -> None:
    actions.set(fail=_CONFLICT | {"detail": "Session 'sess-1' has ended"})
    got = actions.run('NV_doPark("w1", "sess-1", refetch, toast)')
    assert got["result"] == {"failed": True}
    assert got["toasts"] == [["Park failed: Session 'sess-1' has ended", {"kind": "error", "requestId": "req-409"}]]
    assert got["refetches"] == 0


# --- End ------------------------------------------------------------------------------------------------------------------


def test_end_asks_first_and_names_the_session_and_what_it_does(actions) -> None:
    got = actions.run('NV_doEnd("w1", "sess-1", "Quarterly numbers", refetch, toast)')
    confirm = got["calls"][0]
    assert confirm[0] == "confirm"
    assert confirm[1]["danger"] is True and confirm[1]["confirmLabel"] == "End session", "the button is named after the action"
    assert "Quarterly numbers" in confirm[1]["message"]
    assert "running turn" in confirm[1]["message"] and "reopens" in confirm[1]["message"], (
        "it says a running turn is cancelled and that a message later reopens the session"
    )
    assert got["calls"][1] == ["cancel", "w1", "sess-1"]
    assert got["result"] == {"ok": True}
    assert got["toasts"] == [["Session ended", None]]
    assert got["refetches"] == 1


def test_end_declined_does_nothing(actions) -> None:
    actions.set(confirm=False)
    got = actions.run('NV_doEnd("w1", "sess-1", "x", refetch, toast)')
    assert [c[0] for c in got["calls"]] == ["confirm"], "no request after a Cancel"
    assert got["result"] == {"cancelled": True}
    assert got["toasts"] == [] and got["refetches"] == 0


def test_end_that_fails_says_so_instead_of_staying_silent(actions) -> None:
    """A 409 on an already-ended session used to vanish."""
    actions.set(fail=_CONFLICT | {"detail": "Session 'sess-1' has ended"})
    got = actions.run('NV_doEnd("w1", "sess-1", "x", refetch, toast)')
    assert got["result"] == {"failed": True}
    assert got["toasts"] == [["End failed: Session 'sess-1' has ended", {"kind": "error", "requestId": "req-409"}]]
    assert got["refetches"] == 1, "the row is re-read so a stale 'running' chip catches up with what the server says"


# --- Delete ---------------------------------------------------------------------------------------------------------------


def test_delete_confirms_with_a_delete_button_and_reports_the_result(actions) -> None:
    got = actions.run('NV_doDelete("w1", "sess-1", "Quarterly numbers", onDeleted, toast)')
    confirm = got["calls"][0]
    assert confirm[1]["danger"] is True and confirm[1]["confirmLabel"] == "Delete", "not the generic 'Confirm'"
    assert "Quarterly numbers" in confirm[1]["message"]
    assert got["calls"][1] == ["delete", "w1", "sess-1"]
    assert got["result"] == {"ok": True} and got["deleted"] == 1
    assert got["toasts"] == [["Session deleted", None]]


def test_delete_declined_does_nothing(actions) -> None:
    actions.set(confirm=False)
    got = actions.run('NV_doDelete("w1", "sess-1", "x", onDeleted, toast)')
    assert [c[0] for c in got["calls"]] == ["confirm"]
    assert got["result"] == {"cancelled": True} and got["deleted"] == 0 and got["toasts"] == []


def test_deleting_a_running_session_says_why_and_leaves_the_session_alone(actions) -> None:
    """The server answers 409 and the console showed nothing: the tab stayed and the turn kept running, as if the click had been lost."""
    actions.set(fail=_CONFLICT)
    got = actions.run('NV_doDelete("w1", "sess-1", "x", onDeleted, toast)')
    assert got["result"] == {"failed": True}
    assert got["deleted"] == 0, "the tab is not closed for a session that still exists"
    assert got["toasts"] == [
        ["Delete failed: Session 'sess-1' is running; end it before deleting it", {"kind": "error", "requestId": "req-409"}],
    ]


def test_an_error_without_a_detail_falls_back_to_its_message(actions) -> None:
    actions.set(fail={"message": "Failed to fetch"})
    got = actions.run('NV_doPark("w1", "sess-1", refetch, toast)')
    assert got["toasts"] == [["Park failed: Failed to fetch", {"kind": "error", "requestId": None}]]


# --- where the helpers are used -------------------------------------------------------------------------------------------


def test_the_rail_menu_goes_through_the_helpers_and_no_longer_fires_bare_requests() -> None:
    start = RAIL.index("function NV_Rail_SessionContextMenu")
    menu = RAIL[start:RAIL.index("\n}\n", start)]
    for helper in ("window.NV_doPark(", "window.NV_doEnd(", "window.NV_doDelete("):
        assert helper in menu, helper
    for bare in ("SH_api.pause(", "SH_api.cancel(", "SH_api.deleteSession("):
        assert bare not in menu, bare + " fired with no failure path"


def test_the_helpers_are_exported_for_the_rail() -> None:
    for name in ("NV_doPark", "NV_doEnd", "NV_doDelete"):
        assert "window." + name + " = " + name + ";" in DOC


def test_the_verb_allowlist_has_the_words_the_new_verbs_open_with() -> None:
    assert '"End"' in VERBS
