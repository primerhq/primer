"""The shell carries a view's third segment, the provider type, through ``goView``, the context and the address bar (Platform revamp Plan 01, Task 6; seam I-1).

``con.goView(name, nav, sub)`` sets ``view = {name, nav, sub}`` with one history entry; ``con.view`` defaults to ``{name: "studio", nav: null, sub: null}``; and the effect that writes the
address bar depends on ``view.sub``, or a move from one provider type to another would change the state and leave the address bar where it was. The phone's and the desktop's Platform pages
reach providers through ``goView("platform", "providers", <type>)``, so the real ``goView`` source is run here, in the manner of ``tests/ui/test_shell_clear_view.py``: a harness stand-in for
``goView`` would pass a shell whose ``goView`` drops the third argument.
"""

from __future__ import annotations

import json
from pathlib import Path

SHELL = (Path(__file__).resolve().parents[2] / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
START = "  var goView = React.useCallback(function (name, nav, sub) {"
END = "}, [setView, setOpenMenu, markPush]);"


def _real_go_view_source() -> str:
    start = SHELL.index(START)
    return SHELL[start:SHELL.index(END, start) + len(END)]


def _run(call: str) -> list:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        out = ctx.eval(
            "(function () { var calls = [];"
            " var setView = function (v) { calls.push(['setView', v]); };"
            " var markPush = function () { calls.push(['markPush']); };"
            " var setOpenMenu = function (v) { calls.push(['setOpenMenu', v]); };"
            " var React = { useCallback: function (fn) { return fn; } };"
            + _real_go_view_source() +
            f" {call}; return JSON.stringify(calls); }})()"
        )
        return json.loads(out)
    finally:
        ctx.close()


def test_go_view_sets_the_type_with_one_history_entry() -> None:
    calls = _run('goView("platform", "providers", "web_search")')
    assert calls.count(["markPush"]) == 1, "one navigation is one history entry"
    assert ["setView", {"name": "platform", "nav": "providers", "sub": "web_search"}] in calls
    assert calls.index(["markPush"]) < calls.index(["setView", {"name": "platform", "nav": "providers", "sub": "web_search"}]), "the push is marked before the state moves"
    assert ["setOpenMenu", None] in calls, "a menu that was open closes, as it always did"


def test_go_view_without_a_type_records_a_null_one() -> None:
    assert ["setView", {"name": "platform", "nav": "agents", "sub": None}] in _run('goView("platform", "agents")')
    assert ["setView", {"name": "studio", "nav": None, "sub": None}] in _run('goView("studio")')
    assert ["setView", {"name": "platform", "nav": "providers", "sub": None}] in _run('goView("platform", "providers", "")'), "an empty type is no type"


def test_the_url_write_effect_depends_on_the_type() -> None:
    effect = SHELL[SHELL.index("// --- URL sync: write when our state moved"):]
    deps = effect[effect.index("}, [wid, view && view.name"):]
    deps = deps[:deps.index("]);") + 3]
    assert "view && view.sub" in deps, deps
    assert deps.index("view && view.nav") < deps.index("view && view.sub"), "the type is the third segment: after the nav"


def test_the_write_effect_hands_the_whole_view_to_the_builder() -> None:
    """The builder reads the type off the view object it is given, so the effect must give it the view and not a rebuilt ``{name, nav}``."""
    effect = SHELL[SHELL.index("// --- URL sync: write when our state moved"):]
    build = effect[:effect.index("var current = window.location.hash")]
    assert "wid: wid, view: view, doc: doc, overlay: overlay, anchor: anchor," in build


def test_the_default_view_carries_a_null_type() -> None:
    assert 'view: view || { name: "studio", nav: null, sub: null },' in SHELL


def test_go_view_is_what_the_context_hands_to_the_surfaces() -> None:
    assert "goView: goView," in SHELL
