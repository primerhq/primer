"""Always-on status verb and scroll anchoring (spec section 8).

Both are antipattern guards as much as features: a bare spinner and a
force-follow scroll are explicitly prohibited, and the SAME string must
render at all three altitudes, which is only guaranteed if there is one
function producing it.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-status.js"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def test_status_line_is_verb_object_elapsed() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_statusLine({verb: "grep", object: "src/", elapsedSec: 12})'
    ) == "running: grep src/ — 12s"


def test_a_run_with_no_tool_yet_still_says_something() -> None:
    """Prohibited: bare spinners and silent pre-first-token gaps."""
    ctx = _ctx()
    assert ctx.eval("SH_statusLine({elapsedSec: 0})") == "running: thinking — 0s"


def test_long_runs_read_in_minutes() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_statusLine({verb: "run", object: "pytest", elapsedSec: 62})'
    ) == "running: run pytest — 1m 02s"


# ---------------------------------------------------------------------------
# UX reconcile wave 1 (audit A item 10): the parked-session status line.
# ---------------------------------------------------------------------------


def test_wait_line_names_the_tool_for_an_approval() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_waitLine({kind: "approval", gatedTool: "workspace__write"})'
    ) == "waiting on approval — workspace__write (parked, worker released)"


def test_wait_line_reads_as_an_answer_for_a_question() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_waitLine({kind: "question", gatedTool: "ask_user"})'
    ) == "waiting on your answer — ask_user (parked, worker released)"


def test_wait_line_falls_back_without_a_tool_name() -> None:
    ctx = _ctx()
    assert ctx.eval('SH_waitLine({kind: "approval"})') == (
        "waiting on approval — a tool call (parked, worker released)"
    )
    assert ctx.eval('SH_waitLine({kind: "question"})') == (
        "waiting on your answer — ask_user (parked, worker released)"
    )


def test_wait_line_names_the_gated_tool_not_the_yield_kind() -> None:
    """Live finding 01a064d3: item.toolName is the yield KIND
    ("approval"/"ask_user" - shell-attention.js's SH_tierFor tier
    routing needs exactly that literal), never the actual gated tool -
    but the status strip used to read toolName anyway and rendered
    "waiting on approval — approval" for every approval gate regardless
    of which tool it gated. Pin that a stale/irrelevant toolName is
    ignored in favor of gatedTool (shell-attention.js's SH_gatedToolOf)."""
    ctx = _ctx()
    assert ctx.eval(
        'SH_waitLine({kind: "approval", toolName: "approval", '
        'gatedTool: "workspace__write"})'
    ) == "waiting on approval — workspace__write (parked, worker released)"


def test_wait_line_is_null_with_nothing_parked() -> None:
    ctx = _ctx()
    assert ctx.eval("SH_waitLine(null)") is None


# ---------------------------------------------------------------------------
# SH_parkedStatusLine - the full decision nv-session-doc.jsx's waitNote
# prop needs (gate item vs. the wake/timer fallback), a one-argument-
# shape drop-in for its current inline expression.
# ---------------------------------------------------------------------------


def test_parked_status_line_is_null_when_not_parked() -> None:
    ctx = _ctx()
    assert ctx.eval("SH_parkedStatusLine({parked_status: null}, [])") is None
    assert ctx.eval("SH_parkedStatusLine(null, [])") is None


def test_parked_status_line_is_null_once_the_session_has_ended() -> None:
    """Live finding 01a064d3: a sweep/timeout continuation that then
    fails can end a session without clearing parked_status - the
    composer used to keep showing "waiting on your answer — ask_user
    (parked, worker released)" underneath an "Ended" header on the SAME
    session. A stale parked_status must never outrank status: "ended"."""
    ctx = _ctx()
    assert ctx.eval(
        'SH_parkedStatusLine({parked_status: "parked", status: "ended"}, '
        '[{kind: "question", gatedTool: "ask_user"}])'
    ) is None


def test_parked_status_line_names_the_approval_gate() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_parkedStatusLine({parked_status: "parked"}, '
        '[{kind: "approval", gatedTool: "workspace__write"}])'
    ) == "waiting on approval — workspace__write (parked, worker released)"


def test_parked_status_line_names_the_ask_gate() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_parkedStatusLine({parked_status: "parked"}, '
        '[{kind: "question", gatedTool: "ask_user"}])'
    ) == "waiting on your answer — ask_user (parked, worker released)"


def test_parked_status_line_falls_back_with_no_gate_item() -> None:
    """A wake/timer park (sleep, watch_files, ...) carries no decision
    gate - keep the existing wording for that case rather than inventing
    one, per the wave-1 brief."""
    ctx = _ctx()
    assert ctx.eval(
        'SH_parkedStatusLine({parked_status: "parked"}, [])'
    ) == "parked — waiting on a wake"
    assert ctx.eval(
        'SH_parkedStatusLine({parked_status: "parked"}, null)'
    ) == "parked — waiting on a wake"


def test_status_is_derived_from_the_latest_tool_call_on_the_tap() -> None:
    """Timestamps are ISO strings, as TapEvent sends them.

    This test used to build events with a ``ts_ms`` number, a field the
    real event has never had. It passed while the product was broken:
    the status line read that same absent field, fell back to 0 and
    reported the age of the Unix epoch as the elapsed time.
    """
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var evs = [
            {"class": "user_input", session_id: "s1", seq: 1,
             ts: "1970-01-01T00:00:01.000Z", payload: {}},
            {"class": "tool_call", session_id: "s1", seq: 2,
             ts: "1970-01-01T00:00:02.000Z",
             payload: {name: "workspace__grep", arguments: {path: "src/"}}},
            {"class": "tool_call", session_id: "s2", seq: 9,
             ts: "1970-01-01T00:00:02.500Z",
             payload: {name: "workspace__read_file", arguments: {path: "x"}}}
          ];
          return JSON.stringify(SH_statusFromTap(evs, "s1", 14000));
        })()
        """
    ))
    assert out == {"verb": "grep", "object": "src/", "startedMs": 2000}


def test_a_terminated_session_has_no_status() -> None:
    ctx = _ctx()
    assert ctx.eval(
        """
        SH_statusFromTap([
          {"class": "tool_call", session_id: "s1", seq: 2,
           ts: "1970-01-01T00:00:02.000Z",
           payload: {name: "workspace__grep", arguments: {}}},
          {"class": "done", session_id: "s1", seq: 3,
           ts: "1970-01-01T00:00:03.000Z", payload: {}}
        ], "s1", 9000) === null
        """
    ) is True


def test_auto_follow_only_near_the_bottom() -> None:
    ctx = _ctx()
    near = json.loads(ctx.eval(
        'JSON.stringify(SH_scrollDecision({distanceFromBottom: 40, newTurns: 2}))'
    ))
    assert near == {"follow": True, "showJump": False, "jumpLabel": None}


def test_scrolling_up_freezes_and_offers_the_jump() -> None:
    ctx = _ctx()
    far = json.loads(ctx.eval(
        'JSON.stringify(SH_scrollDecision({distanceFromBottom: 900, newTurns: 3}))'
    ))
    assert far == {
        "follow": False,
        "showJump": True,
        "jumpLabel": "Jump to latest - 3 new turns",
    }
    one = json.loads(ctx.eval(
        'JSON.stringify(SH_scrollDecision({distanceFromBottom: 900, newTurns: 1}))'
    ))
    assert one["jumpLabel"] == "Jump to latest - 1 new turn"
    none = json.loads(ctx.eval(
        'JSON.stringify(SH_scrollDecision({distanceFromBottom: 900, newTurns: 0}))'
    ))
    assert none == {"follow": False, "showJump": False, "jumpLabel": None}


def test_the_status_clock_reads_the_field_the_tap_actually_sends() -> None:
    """Regression: a session that had just started said "29787968m 43s".

    TapEvent carries ``ts``, an ISO datetime. The status line read
    ``ts_ms``, which that event does not have, so every start time fell
    back to 0 and the elapsed clock measured from the Unix epoch.
    """
    from primer.tap.event import TapEvent

    assert "ts" in TapEvent.model_fields
    assert "ts_ms" not in TapEvent.model_fields

    ctx = _ctx()
    ctx.eval(
        """
        var now = 1750000000000;
        var evs = [{
          session_id: "s1", "class": "user_input",
          ts: "2025-06-15T14:26:20.000Z", payload: {}
        }];
        var st = SH_statusFromTap(evs, "s1", now);
        var elapsed = Math.round((now - st.startedMs) / 1000);
        // An event with no usable timestamp starts now, so the clock
        // reads zero rather than fifty-five years.
        var noTs = SH_statusFromTap(
          [{session_id: "s1", "class": "user_input", payload: {}}], "s1", now);
        """
    )
    assert ctx.eval("st.verb") == "thinking"
    # Under a minute of elapsed time, not the age of the epoch.
    assert 0 <= ctx.eval("elapsed") < 3600, ctx.eval("elapsed")
    assert ctx.eval("noTs.startedMs") == 1750000000000
