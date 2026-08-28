"""Scroll-follow (Phase 3): the pure follow state machine.

The module (ui/foundation/scroll-follow.js) is loaded here exactly like
test_session_store.py loads session-store.js: a MiniRacer context with
`var window = globalThis;`, no DOM. We drive the PURE core (SF_init /
SF_measure) through window.* and inspect the state + actions, so the
logic is unit-tested without a real scroll container.

Covers plan Phase 3.3 + the assistant-ui scroll findings (assistant-ui.md
2.4): user-scroll-up detection (scrollTop down while scrollHeight
unchanged), content growth never disabling follow, return-to-bottom, the
programmatic-scroll guard, pointerdown cancelling the pending pin, the
distance threshold boundary, and the newContentWhileAway count + reset.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "scroll-follow.js"


def _ctx() -> "py_mini_racer.MiniRacer":
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _g(scroll_top, scroll_height, client_height, source):
    """A geometry frame for SF_measure."""
    return {
        "scrollTop": scroll_top,
        "scrollHeight": scroll_height,
        "clientHeight": client_height,
        "source": source,
    }


def _run(ctx, geoms, follow_px=100):
    """Apply `geoms` to a fresh state, returning the final state, the
    per-step actions, and the per-step state snapshots."""
    out = ctx.eval(
        "(function () {\n"
        "  var s = SF_init({ followPx: " + str(int(follow_px)) + " });\n"
        "  var geoms = " + json.dumps(geoms) + ";\n"
        "  var actions = [];\n"
        "  var states = [];\n"
        "  for (var i = 0; i < geoms.length; i++) {\n"
        "    var res = SF_measure(s, geoms[i]);\n"
        "    s = res.state;\n"
        "    actions.push(res.actions);\n"
        "    states.push(s);\n"
        "  }\n"
        "  return JSON.stringify({ state: s, actions: actions, states: states });\n"
        "})()"
    )
    return json.loads(out)


def test_user_scroll_up_mid_growth_disengines_and_shows_pill() -> None:
    """User pulling up while content grows: follow off + jump pill on."""
    ctx = _ctx()
    r = _run(ctx, [
        _g(1500, 2000, 500, "scroll"),    # at bottom (distance 0)
        _g(1500, 2500, 500, "content"),   # content grows; follow stays on
        _g(1200, 2500, 500, "scroll"),    # user pulls up, height unchanged
    ])
    assert r["states"][1]["followBottom"] is True    # content did not disable
    assert r["state"]["followBottom"] is False       # the pull-up did
    assert r["actions"][2]["showJumpPill"] is True


def test_content_growth_alone_never_disables_follow() -> None:
    ctx = _ctx()
    r = _run(ctx, [
        _g(1500, 2000, 500, "scroll"),
        _g(1500, 2500, 500, "content"),   # growth, not a user gesture
        _g(1500, 3000, 500, "content"),   # more growth
    ])
    assert r["state"]["followBottom"] is True
    assert r["actions"][1]["scrollToBottom"] is True   # pin while following
    assert r["actions"][1]["showJumpPill"] is False


def test_return_to_bottom_reengines_follow_and_hides_pill() -> None:
    ctx = _ctx()
    r = _run(ctx, [
        _g(1500, 2000, 500, "scroll"),    # at bottom
        _g(1000, 2000, 500, "scroll"),    # pull up -> follow off
        _g(1500, 2000, 500, "scroll"),    # back to the bottom
    ])
    assert r["states"][1]["followBottom"] is False
    assert r["state"]["followBottom"] is True
    assert r["state"]["newContentWhileAway"] == 0
    assert r["actions"][2]["showJumpPill"] is False


def test_programmatic_scroll_does_not_flip_follow() -> None:
    """A programmatic scroll that lands off-bottom must not read as the
    user pulling away: followBottom is preserved."""
    ctx = _ctx()
    r = _run(ctx, [
        _g(1500, 2000, 500, "scroll"),    # at bottom, follow on
        _g(1200, 2000, 500, "programmatic"),  # off-bottom, but programmatic
    ])
    assert r["state"]["followBottom"] is True
    # A regular scroll in the same geometry WOULD read as a user pull-up.
    plain = _run(ctx, [
        _g(1500, 2000, 500, "scroll"),
        _g(1200, 2000, 500, "scroll"),
    ])
    assert plain["state"]["followBottom"] is False


def test_pointerdown_cancels_the_pending_pin() -> None:
    ctx = _ctx()
    r = _run(ctx, [
        _g(1500, 2000, 500, "scroll"),       # at bottom
        _g(1500, 2500, 500, "content"),       # growth -> pendingPin set
        _g(1500, 2500, 500, "pointerdown"),  # touch cancels the intent
    ])
    assert r["states"][1]["pendingPin"] is True    # growth queued a pin
    assert r["state"]["pendingPin"] is False        # pointerdown cleared it


def test_distance_threshold_boundary() -> None:
    """distance == followPx is at bottom; followPx + 1 is not."""
    ctx = _ctx()
    at = _run(ctx, [_g(1500, 2100, 500, "scroll")])   # distance 100
    just = _run(ctx, [_g(1499, 2100, 500, "scroll")])  # distance 101
    assert at["state"]["atBottom"] is True
    assert just["state"]["atBottom"] is False


def test_new_content_while_away_counts_and_resets() -> None:
    ctx = _ctx()
    r = _run(ctx, [
        _g(1500, 2000, 500, "scroll"),    # at bottom
        _g(1000, 2000, 500, "scroll"),    # pull up -> follow off
        _g(1000, 2500, 500, "content"),   # growth while away -> 1
        _g(1000, 3000, 500, "content"),   # growth while away -> 2
        _g(2500, 3000, 500, "scroll"),    # back to the bottom -> reset
    ])
    assert r["states"][2]["newContentWhileAway"] == 1
    assert r["states"][3]["newContentWhileAway"] == 2
    assert r["state"]["newContentWhileAway"] == 0
    assert r["state"]["followBottom"] is True


def test_init_reads_the_status_constant() -> None:
    """SF_init defaults followPx to window.SH_FOLLOW_PX when present."""
    ctx = _ctx()
    ctx.eval("window.SH_FOLLOW_PX = 42;")
    out = json.loads(ctx.eval("JSON.stringify(SF_init())"))
    assert out["options"]["followPx"] == 42
    # A bare followPx is still the fallback constant.
    bare = _ctx()
    bare.eval("window.SH_FOLLOW_PX = 100;")
    o = json.loads(bare.eval("JSON.stringify(SF_init())"))
    assert o["options"]["followPx"] == 100
