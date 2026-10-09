"""The optimistic "sending" leg is dropped when the session SETTLES, not only when it ENDS (follow-up of #664, review of the C-024 console part).

Sending a message sets an optimistic flag so the composer says "sending" and the poll keeps going until the tap shows life: it is cleared when a live turn shows, or, as a bounded fallback,
when the polled row confirms the session settled with no live turn. That fallback only fired for an ENDED session (``NV_sessionIsOver``). Since #623 a failed send RESTS the session
(``status`` waiting, ``turn_status`` idle, ``last_turn_error`` on the row), so if the tap's frames arrive before the POST's response the leg is never cleared: the composer stays on
"sending" and the failure note stays hidden until a reload. ``NV_optimisticLegOver`` is the rule, a pure function of the polled row.

Round 2 of the review: the fallback only ran when the polled row CHANGED, and ``useResource`` does not re-emit a structurally equal poll (``use-resource.js``), so a turn that settled within the
ten seconds was checked once, too early, and never again: the leg stayed on "sending". It is TIME-DRIVEN now (``NV_useOptimisticLegDrop``: drop at once if the window is over and the row settled,
else look again when the window closes), tested below with the real ``use-resource.js``, the mini React and a fake clock.

Also (a nit of the same review): the end note of a session that ended after a raised, non-transport failure names the failure code the row stamped when its ``ended_detail`` is empty, and so does
the sweeper's ``failure_exit_unfinished`` (``primer/bus/scheduler_tasks.py``: a failure exit that stamped the row and did not finish), but only then: a real ``ended_detail`` wins over the stamp.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
USE_RESOURCE = (ROOT / "ui" / "foundation" / "use-resource.js").read_text(encoding="utf-8")
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


@pytest.fixture
def v8():
    from py_mini_racer import MiniRacer

    made: list = []

    def build(*names: str):
        ctx = MiniRacer()
        made.append(ctx)
        for name in names:
            start = DOC.index("function " + name)
            ctx.eval(DOC[start:DOC.index("\n}\n", start) + len("\n}\n")])
        return ctx

    try:
        yield build
    finally:
        for c in made:
            c.close()


def _over(ctx, session, optimistic=1_000, now=12_000):
    return json.loads(ctx.eval(f"JSON.stringify(NV_optimisticLegOver({json.dumps(session)}, {optimistic}, {now}))"))


ENDED = {"status": "ended", "ended_reason": "failed", "turn_status": "idle"}
RESTING = {"status": "waiting", "ended_reason": None, "turn_status": "idle", "session_state": "parked", "last_turn_error": {"code": "server_error", "at": "t"}}


def test_a_settled_ended_session_drops_the_leg_after_ten_seconds(v8) -> None:
    """The control: the case the fallback always covered."""
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), ENDED) is True


def test_a_settled_resting_session_drops_the_leg_after_ten_seconds(v8) -> None:
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), RESTING) is True


def test_a_session_that_rests_without_a_failure_drops_it_too(v8) -> None:
    """Any settled row: a message that never reached a turn leaves the session waiting with nothing on it."""
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), {"status": "waiting", "turn_status": "idle", "session_state": "parked"}) is True


@pytest.mark.parametrize("session", [ENDED, RESTING], ids=["ended", "resting"])
def test_nothing_is_dropped_before_the_ten_seconds_are_up(v8, session: dict) -> None:
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), session, optimistic=5_000, now=12_000) is False


@pytest.mark.parametrize("turn_status", ["running", "claimable"])
def test_a_turn_that_is_queued_or_running_keeps_the_leg(v8, turn_status: str) -> None:
    assert _over(v8("NV_sessionIsOver", "NV_optimisticLegOver"), {**RESTING, "turn_status": turn_status}) is False


def test_no_leg_and_no_row_are_nothing_to_drop(v8) -> None:
    ctx = v8("NV_sessionIsOver", "NV_optimisticLegOver")
    assert _over(ctx, RESTING, optimistic=0) is False
    assert _over(ctx, None) is False


def test_a_message_to_a_parked_session_drops_the_leg_after_ten_seconds_and_leaves_the_queued_chip_to_the_row(v8) -> None:
    """Pinned (review nit): a message to a PARKED session is routed pending, so the row stays idle while the session is parked; the leg (the composer's "sending") drops after the window, and what
    says the message is queued is the row's own state (the chip), not this flag."""
    parked = {"status": "waiting", "ended_reason": None, "turn_status": "idle", "session_state": "parked", "parked_status": "parked", "last_turn_error": None}
    ctx = v8("NV_sessionIsOver", "NV_optimisticLegOver")
    assert _over(ctx, parked, optimistic=1_000, now=12_000) is True
    assert _over(ctx, parked, optimistic=1_000, now=9_000) is False


# ---------------------------------------------------------------------------
# the drop is time-driven: the real useResource (it does not re-emit an equal poll), the mini React and a fake clock
# ---------------------------------------------------------------------------

_CLOCK = r"""
var __now = 1000;
Date.now = function () { return __now; };
var __timers = []; var __tid = 0;
function setTimeout(fn, ms) { __tid += 1; __timers.push({ id: __tid, at: __now + (ms || 0), fn: fn }); return __tid; }
function clearTimeout(id) { __timers = __timers.filter(function (t) { return t.id !== id; }); }
globalThis.setTimeout = setTimeout; globalThis.clearTimeout = clearTimeout;
globalThis.document = { hidden: false, addEventListener: function () {} };
globalThis.AbortController = function () { this.signal = {}; this.abort = function () {}; };
function __advance(ms) {
  var until = __now + ms;
  for (;;) {
    var due = __timers.filter(function (t) { return t.at <= until; }).sort(function (a, b) { return a.at - b.at; })[0];
    if (!due) break;
    __timers = __timers.filter(function (t) { return t !== due; });
    __now = due.at;
    due.fn();
  }
  __now = until;
}
"""

_PROBE = r"""
var __row = null; var __dropped_at = [];
function Probe(props) {
  var res = window.primerApi.useResource("probe-row", function () { return Promise.resolve(JSON.parse(JSON.stringify(__row))); }, { pollMs: 2000 });
  var st = React.useState(props.optimistic); var optimistic = st[0]; var setOptimistic = st[1];
  NV_useOptimisticLegDrop(res.data, optimistic, function () { __dropped_at.push(Date.now()); setOptimistic(null); });
  return null;
}
function Blank() { return null; }
"""

SETTLED = {"status": "waiting", "turn_status": "idle", "session_state": "parked", "last_turn_error": {"code": "server_error", "at": "t1"}}
RUNNING = {"status": "running", "turn_status": "running", "session_state": "running", "last_turn_error": None}


def _names(*names: str) -> str:
    parts = []
    for name in names:
        start = DOC.find("function " + name + "(")
        assert start >= 0, f"{name} is not defined in nv-session-doc.jsx"
        parts.append(DOC[start:DOC.index("\n}\n", start) + len("\n}\n")])
    return "\n".join(parts)


def _scenario(steps: list[tuple[str, object]]) -> dict:
    """Run the steps ([("row", dict) | ("mount", optimistic_ms) | ("advance", ms) | ("unmount", None)]) and say when the leg was dropped and what timers are left."""
    code = _names("NV_optimisticLegLeftMs", "NV_optimisticLegOver", "NV_useOptimisticLegDrop") + "\n" + USE_RESOURCE + "\n" + _PROBE
    ctx = mini_react_context(code, prelude=_CLOCK)
    mounted = False
    try:
        for kind, arg in steps:
            if kind == "row":
                ctx.eval("__row = " + json.dumps(arg) + ";")
            elif kind == "mount":
                ctx.eval("MR.mount(Probe, {optimistic: " + str(arg) + "});")
            elif kind == "advance":
                ctx.eval("__advance(" + str(arg) + ");")
            elif kind == "unmount":
                ctx.eval("MR.mount(Blank, {});")
            ctx.eval("void 0;")   # let the fetch's promise jobs run
            if kind == "mount":
                mounted = True
            if mounted:
                ctx.eval("MR.rerender();")
        return json.loads(ctx.eval("JSON.stringify({now: __now, dropped: __dropped_at, timers: __timers.length})"))
    finally:
        ctx.close()


def _advance(seconds: int) -> list[tuple[str, object]]:
    return [("advance", 2000)] * (seconds // 2)


def test_a_turn_that_settled_within_the_window_drops_the_leg_when_the_window_closes_though_every_poll_is_identical() -> None:
    """The reviewer's race: the POST resolved at t=1000, the turn had already settled (rested), every poll returns the SAME row, so ``useResource`` never re-emits it. The leg must still go."""
    out = _scenario([("row", SETTLED), ("mount", 1000), *_advance(30)])
    assert len(out["dropped"]) == 1, out
    assert 11_000 < out["dropped"][0] <= 12_100, ("dropped when the ten seconds were up, not before and not late", out)
    assert out["timers"] <= 1, "only the poll timer is left, not a drop timer"


def test_a_turn_that_settles_after_the_window_drops_the_leg_when_the_row_changes() -> None:
    out = _scenario([("row", RUNNING), ("mount", 1000), *_advance(10), ("row", SETTLED), *_advance(6)])
    assert len(out["dropped"]) == 1 and out["dropped"][0] > 11_000, out


def test_a_turn_that_is_still_running_keeps_the_leg_however_long_it_takes() -> None:
    out = _scenario([("row", RUNNING), ("mount", 1000), *_advance(40)])
    assert out["dropped"] == [], out


def test_a_row_that_changes_inside_the_window_is_judged_again_at_the_end_of_it() -> None:
    """Running for the first four seconds, settled after: the drop is at the end of the ten seconds, not at the change and not never."""
    out = _scenario([("row", RUNNING), ("mount", 1000), *_advance(4), ("row", SETTLED), *_advance(14)])
    assert len(out["dropped"]) == 1 and 11_000 < out["dropped"][0] <= 12_100, out


def test_a_row_that_settled_long_before_the_leg_was_set_drops_it_at_once() -> None:
    """The control for the other path: the window is already over when the effect runs, so no timer is needed."""
    out = _scenario([("row", SETTLED), ("advance", 20_000), ("mount", 1000)])
    assert len(out["dropped"]) == 1 and out["dropped"][0] <= 21_100, out


def test_a_leg_that_is_gone_or_a_box_that_is_unmounted_leaves_no_timer_behind() -> None:
    gone = _scenario([("row", SETTLED), ("mount", 0), *_advance(30)])
    assert gone["dropped"] == [] and gone["timers"] <= 1, gone
    away = _scenario([("row", SETTLED), ("mount", 1000), ("advance", 2000), ("unmount", None), ("advance", 20_000)])
    assert away["dropped"] == [], "an unmounted box must not drop (or fire) anything"


def test_the_document_uses_the_hook_and_not_an_effect_of_its_own() -> None:
    assert "NV_useOptimisticLegDrop(session, optimistic, function () { setOptimistic(null); });" in DOC
    assert "if (NV_optimisticLegOver(session, optimistic, Date.now())) setOptimistic(null);" not in DOC, "the one-shot effect on [detail.data] is gone"


def test_the_end_note_names_the_stamped_code_when_the_row_has_no_detail(v8) -> None:
    ctx = v8("NV_failureWords", "NV_failureCodeWords", "NV_endedLine")
    row = {"status": "ended", "ended_reason": "failed", "ended_detail": None, "last_turn_error": {"code": "weird_code_42", "at": "t"}}
    got = json.loads(ctx.eval(f"JSON.stringify(NV_endedLine({json.dumps(row)}))"))
    assert "the failure code is weird_code_42" in got["why"], got
    bare = json.loads(ctx.eval(f"JSON.stringify(NV_endedLine({json.dumps({**row, 'last_turn_error': {'code': 'turn_failed', 'at': 't'}})}))"))
    assert bare["why"] == "The turn failed; the error is in the transcript above.", "turn_failed names nothing a person can act on"
    none = json.loads(ctx.eval(f"JSON.stringify(NV_endedLine({json.dumps({**row, 'last_turn_error': None})}))"))
    assert none["why"] == "The turn failed; the error is in the transcript above."


def _ended(ctx, **fields) -> dict:
    row = {"status": "ended", "ended_reason": "failed", "ended_detail": None, "last_turn_error": None, **fields}
    return json.loads(ctx.eval(f"JSON.stringify(NV_endedLine({json.dumps(row)}))"))


def test_the_sweepers_failure_exit_unfinished_names_the_stamped_code_too(v8) -> None:
    """``primer/bus/scheduler_tasks.py`` ends a row whose failure exit stamped it and did not finish with ``ended_detail`` ``failure_exit_unfinished``; the stamp is the real cause."""
    ctx = v8("NV_failureWords", "NV_failureCodeWords", "NV_endedLine")
    words = json.loads(ctx.eval('JSON.stringify(NV_failureWords("server_error"))'))
    got = _ended(ctx, ended_detail="failure_exit_unfinished", last_turn_error={"code": "server_error", "at": "t"})
    assert got["why"] == "It failed: " + words["what"] + "." and got["next"] == words["next"], got
    unknown = _ended(ctx, ended_detail="failure_exit_unfinished", last_turn_error={"code": "weird_code_42", "at": "t"})
    assert "the failure code is weird_code_42" in unknown["why"], unknown


def test_the_sweepers_marker_with_nothing_stamped_or_turn_failed_stamped_says_what_it_knows(v8) -> None:
    ctx = v8("NV_failureWords", "NV_failureCodeWords", "NV_endedLine")
    for stamp in (None, {"code": "turn_failed", "at": "t"}):
        got = _ended(ctx, ended_detail="failure_exit_unfinished", last_turn_error=stamp)
        assert "failure_exit_unfinished" not in got["why"], ("the sweeper's marker is not user language", got)
        assert got["why"].startswith("It failed:") and got["next"], got


@pytest.mark.parametrize("detail", ["tool_execution_failed", "routing_failed", "stream_timeout", "never_started"])
def test_a_real_ended_detail_wins_over_the_stamp(v8, detail: str) -> None:
    """The stamp is the fallback for an EMPTY detail (and the sweeper's marker); it never overrides what the ender wrote."""
    ctx = v8("NV_failureWords", "NV_failureCodeWords", "NV_endedLine")
    stamped = _ended(ctx, ended_detail=detail, last_turn_error={"code": "server_error", "at": "t"})
    plain = _ended(ctx, ended_detail=detail, last_turn_error=None)
    assert stamped == plain, (detail, stamped, plain)
