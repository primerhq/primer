"""S8 palette contract (spec section 8, "Rail, tabs, palette").

Registration is linted (verb-noun Title Case, agent-registered verbs
included), ranking is layered (base weight x fuzzy x dampener) with hard
context gating, and every verb declares at least one pointer surface so
the dual-render rule is checkable statically later.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-verbs.js"
INDEX = ROOT / "ui" / "index.html"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var REG = SH_createVerbRegistry();
        REG.register({
          id: "session.open", label: "Open Session", weight: 1,
          surfaces: ["rail", "palette"], chord: "Ctrl+P", run: function () {}
        });
        REG.register({
          id: "session.park", label: "Park Session", aliases: ["Pause"],
          weight: 1, surfaces: ["tab-menu"], run: function () {}
        });
        REG.register({
          id: "session.delete", label: "Delete Session", weight: 1,
          destructive: true, surfaces: ["tab-menu"], run: function () {}
        });
        REG.register({
          id: "trace.split", label: "Split Right", weight: 1,
          contexts: ["session"], surfaces: ["tab-menu"], run: function () {}
        });
        """
    )
    return ctx


def test_module_registered_and_pure() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert "window.SH_createVerbRegistry" in src
    for banned in ("document.", "window.location", "apiFetch"):
        assert banned not in src, banned
    assert 'src="foundation/shell-verbs.js"' in INDEX.read_text(encoding="utf-8")


def test_lint_accepts_verb_noun_title_case_and_parenthesised_aliases() -> None:
    ctx = _ctx()
    for good in ("Open Session", "Park Session (Pause)", "Split Right",
                 "Switch Binding", "Approve Gate"):
        assert ctx.eval(f"SH_lintVerbLabel({json.dumps(good)}) === null"), good


def test_lint_rejects_the_four_junk_drawer_shapes() -> None:
    ctx = _ctx()
    for bad in ("session", "open session", "Sessions", "open Session"):
        assert ctx.eval(f"SH_lintVerbLabel({json.dumps(bad)}) !== null"), bad


def test_registration_rejects_an_unlinted_label() -> None:
    ctx = _ctx()
    thrown = ctx.eval(
        """
        (function () {
          try {
            REG.register({id: "x", label: "sessions", surfaces: ["rail"],
                          run: function () {}});
            return "";
          } catch (e) { return String(e.message || e); }
        })()
        """
    )
    assert "sessions" in thrown


def test_registration_requires_a_pointer_surface() -> None:
    """Dual-render rule: palette-only is not a registerable shape."""
    ctx = _ctx()
    thrown = ctx.eval(
        """
        (function () {
          try {
            REG.register({id: "y", label: "Open Thing", surfaces: ["palette"],
                          run: function () {}});
            return "";
          } catch (e) { return String(e.message || e); }
        })()
        """
    )
    assert "surface" in thrown.lower()


def test_duplicate_ids_are_rejected() -> None:
    ctx = _ctx()
    thrown = ctx.eval(
        """
        (function () {
          try {
            REG.register({id: "session.open", label: "Open Session",
                          surfaces: ["rail"], run: function () {}});
            return "";
          } catch (e) { return String(e.message || e); }
        })()
        """
    )
    assert "session.open" in thrown


def test_context_gating_is_hard_not_a_ranking_nudge() -> None:
    ctx = _ctx()
    ids = json.loads(ctx.eval(
        'JSON.stringify(SH_rankVerbs(REG, "split", {docKind: "file"})'
        '.map(function (v) { return v.id; }))'
    ))
    assert "trace.split" not in ids
    ids = json.loads(ctx.eval(
        'JSON.stringify(SH_rankVerbs(REG, "split", {docKind: "session"})'
        '.map(function (v) { return v.id; }))'
    ))
    assert ids == ["trace.split"]


def test_destructive_verbs_are_dampened_below_a_safe_peer() -> None:
    ctx = _ctx()
    ids = json.loads(ctx.eval(
        'JSON.stringify(SH_rankVerbs(REG, "session", {})'
        '.map(function (v) { return v.id; }))'
    ))
    assert ids.index("session.delete") > ids.index("session.open")


def test_aliases_match_but_the_canonical_label_is_what_ranks_out() -> None:
    ctx = _ctx()
    rows = json.loads(ctx.eval(
        'JSON.stringify(SH_rankVerbs(REG, "pause", {}).map('
        'function (v) { return [v.id, v.label]; }))'
    ))
    assert rows and rows[0][0] == "session.park"
    assert rows[0][1] == "Park Session (Pause)"


def test_frecency_promotes_a_repeatedly_chosen_target() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var f = SH_createFrecency(function () { return 1000; });
          f.record("session.delete");
          f.record("session.delete");
          f.record("session.delete");
          return JSON.stringify([
            f.scoreFor("session.delete") > f.scoreFor("session.open"),
            f.preferredFor("no-such-query")
          ]);
        })()
        """
    ))
    assert out[0] is True and out[1] is None
    assert ctx.eval(
        """
        (function () {
          var f = SH_createFrecency(function () { return 1000; });
          f.remember("gr", "session.open");
          return f.preferredFor("gr");
        })()
        """
    ) == "session.open"


def test_for_surface_returns_only_verbs_that_declared_it() -> None:
    ctx = _ctx()
    ids = json.loads(ctx.eval(
        'JSON.stringify(REG.forSurface("rail").map(function (v) { return v.id; }))'
    ))
    assert ids == ["session.open"]
