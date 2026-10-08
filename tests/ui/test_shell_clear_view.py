"""The shell's REAL ``clearView`` drops the view and nothing else (review of PR 485).

The phone shell consumes a ``?view=platform:<x>`` link by calling ``con.clearView()`` (``tests/ui/test_mobile_more_reachability.py``). Those V8
tests stand ``clearView`` in with a harness function, so a ``clearView`` in ``nv-shell.jsx`` that does nothing, or that pushes a history entry,
or that sets another view, passes them all and is caught only by the browser journey. This runs the source of the real one.
"""

from __future__ import annotations

import json
from pathlib import Path

SHELL = (Path(__file__).resolve().parents[2] / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
START = "  var clearView = React.useCallback(function () {"
END = "}, [setView]);"


def _real_clear_view_source() -> str:
    start = SHELL.index(START)
    return SHELL[start:SHELL.index(END, start) + len(END)]


def _run(extra: str = "") -> list:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        out = ctx.eval(
            "(function () { var calls = [];"
            " var setView = function (v) { calls.push(['setView', v]); };"
            " var markPush = function () { calls.push(['markPush']); };"
            " var setOpenMenu = function (v) { calls.push(['setOpenMenu', v]); };"
            " var React = { useCallback: function (fn) { return fn; } };"
            + _real_clear_view_source() + extra +
            " clearView(); return JSON.stringify(calls); })()"
        )
        return json.loads(out)
    finally:
        ctx.close()


def test_the_real_clearView_sets_the_view_to_null() -> None:
    assert _run() == [["setView", None]], "clearView must drop the view from state, once, and do nothing else"


def test_the_real_clearView_does_not_push_a_history_entry() -> None:
    """No ``markPush``: the URL write that follows is a replace, so consuming a link adds no entry of its own."""
    assert ["markPush"] not in _run()


def test_clearView_is_what_the_context_hands_to_the_surfaces() -> None:
    assert "clearView: clearView," in SHELL, "the context value exports it"
    # The memo's dependency list is the array that closes the context value; it must name clearView, or a surface would keep a stale one.
    memo = SHELL[SHELL.index("clearView: clearView,"):]
    deps = memo[memo.index("}, ["):memo.index("]);", memo.index("}, ["))]
    assert "clearView" in deps, "and the context memo depends on it"
