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
    ) == "running: grep src/ - 12s"


def test_a_run_with_no_tool_yet_still_says_something() -> None:
    """Prohibited: bare spinners and silent pre-first-token gaps."""
    ctx = _ctx()
    assert ctx.eval("SH_statusLine({elapsedSec: 0})") == "running: thinking - 0s"


def test_long_runs_read_in_minutes() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_statusLine({verb: "run", object: "pytest", elapsedSec: 62})'
    ) == "running: run pytest - 1m 02s"


def test_status_is_derived_from_the_latest_tool_call_on_the_tap() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var evs = [
            {"class": "user_input", session_id: "s1", seq: 1, ts_ms: 1000,
             payload: {}},
            {"class": "tool_call", session_id: "s1", seq: 2, ts_ms: 2000,
             payload: {name: "workspace__grep", arguments: {path: "src/"}}},
            {"class": "tool_call", session_id: "s2", seq: 9, ts_ms: 2500,
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
          {"class": "tool_call", session_id: "s1", seq: 2, ts_ms: 2000,
           payload: {name: "workspace__grep", arguments: {}}},
          {"class": "done", session_id: "s1", seq: 3, ts_ms: 3000, payload: {}}
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
