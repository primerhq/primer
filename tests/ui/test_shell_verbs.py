"""S8 palette contract (spec section 8, "Rail, tabs, palette").

Registration is linted (verb-noun Title Case, agent-registered verbs
included), ranking is layered (base weight x fuzzy x dampener) with hard
context gating, and every verb declares at least one pointer surface so
the dual-render rule is checkable statically later.
"""

from __future__ import annotations

import json
import re
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


# The match tier (lead ruling): a query that is a verb's whole label ranks it first, a prefix of the label next, anything else the fuzzy match lets
# through after them. Fuzzy scores tie whenever two labels share their longest run of characters, so without it registration order picks the winner.

_TIER_VERBS = """
  var T = SH_createVerbRegistry();
  function add(id, label, extra) {
    var verb = Object.assign({ id: id, label: label, surfaces: ["rail", "palette"], run: function () {} }, extra || {});
    T.register(verb);
  }
  function ids(q, c) { return SH_rankVerbs(T, q, c || {}).map(function (v) { return v.id; }); }
"""


def _ids(ctx, query: str, context: str = "{}") -> list[str]:
    return json.loads(ctx.eval("JSON.stringify(ids(" + json.dumps(query) + ", " + context + "))"))


def test_the_whole_label_ranks_first_then_a_prefix_then_the_rest_whatever_the_weights() -> None:
    ctx = _ctx()
    ctx.eval(
        _TIER_VERBS
        + """
        add("loose", "Show Open Tools Panel", { weight: 5 });
        add("prefix", "Open Toolsets");
        add("exact", "Open Tools");
        var f = SH_createFrecency(function () { return 1000; });
        f.record("loose"); f.record("loose"); f.record("loose"); f.remember("open tools", "loose");
        """
    )
    assert _ids(ctx, "open tools") == ["exact", "prefix", "loose"], "no registration order, weight or recorded choice outranks the tier"
    assert _ids(ctx, "open tools", "{frecency: f}") == ["exact", "prefix", "loose"]


def test_the_tier_ignores_case_and_runs_of_spaces() -> None:
    ctx = _ctx()
    ctx.eval(_TIER_VERBS + 'add("prefix", "Open Toolsets"); add("exact", "Open Tools");')
    for query in ("Open Tools", "OPEN TOOLS", "open tools", "  open   tools "):
        assert _ids(ctx, query) == ["exact", "prefix"], query


def test_an_alias_names_a_verb_as_its_label_does() -> None:
    ctx = _ctx()
    ctx.eval(
        _TIER_VERBS
        + """
        add("decoy", "Show Pause");
        add("park", "Park Session", { aliases: ["Pause"] });
        add("later", "Park Sessions Later");
        """
    )
    assert _ids(ctx, "pause") == ["park", "decoy"], "an alias that is the whole query ranks its verb first, ahead of a label that ties with it on the fuzzy score"
    assert _ids(ctx, "paus") == ["park", "decoy"], "and an alias that starts with it is a prefix"
    assert _ids(ctx, "park session") == ["park", "later"], "the label, not the display string with its aliases"


def test_within_a_tier_the_destructive_dampener_still_orders_the_rows() -> None:
    ctx = _ctx()
    ctx.eval(
        _TIER_VERBS
        + """
        add("archive", "Delete Notes Archive", { destructive: true });
        add("later", "Delete Notes Later");
        add("whole", "Delete Notes", { destructive: true });
        """
    )
    assert _ids(ctx, "delete notes") == ["whole", "later", "archive"], (
        "the destructive verb named in full leads (the tier outranks the dampener); within the prefix tier the dampener still puts the safe verb "
        "ahead of the destructive one registered before it"
    )


def test_within_a_tier_frecency_and_the_remembered_choice_still_order_the_rows() -> None:
    ctx = _ctx()
    ctx.eval(
        _TIER_VERBS
        + """
        add("a", "Open Alpha Notes");
        add("b", "Open Alpha Notebook");
        add("c", "Open Alpha");
        var f = SH_createFrecency(function () { return 1000; });
        """
    )
    assert _ids(ctx, "open alpha n", "{frecency: f}") == ["a", "b"], "registration order while nothing separates them"
    ctx.eval('f.record("b"); f.record("b");')
    assert _ids(ctx, "open alpha n", "{frecency: f}") == ["b", "a"], "frecency promotes the verb chosen twice, within the prefix tier"
    assert _ids(ctx, "open alpha", "{frecency: f}")[0] == "c", "but not past the verb named in full"
    ctx.eval('f.remember("open alpha n", "a");')
    assert _ids(ctx, "open alpha n", "{frecency: f}")[0] == "a", "and the remembered choice for the query still outweighs it within the tier"


def test_an_empty_query_has_no_tier_and_keeps_the_scores_then_registration_order() -> None:
    ctx = _ctx()
    ctx.eval(_TIER_VERBS + 'add("a", "Open Alpha"); add("b", "Open Beta"); add("c", "Open Gamma", { weight: 2 }); add("d", "Open Delta");')
    assert _ids(ctx, "") == ["c", "a", "b", "d"], "a heavier verb leads, equal scores stay in registration order"
    ctx.eval("var f = SH_createFrecency(function () { return 1000; }); f.record('d'); f.record('d'); f.record('d');")
    assert _ids(ctx, "", "{frecency: f}")[0] == "d", "frecency still lifts a verb when nothing is typed"


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


# ---------------------------------------------------------------------------
# A verb registered once must still see the shell as it is when it runs.
# ---------------------------------------------------------------------------


def test_a_verb_registered_once_reads_the_current_shell_not_the_first_one() -> None:
    """Regression: every session verb silently did nothing.

    Verbs are registered on the shell's first render and run much later.
    Closing over that render's shell object froze the doc state at mount,
    so a verb resolving its target from ``shell.docs`` found an empty tab
    list. Close Session, Park Session and Interrupt Session all returned
    without sending a request and without saying why.
    """
    ctx = _ctx()
    ctx.eval(
        """
        var ref = { current: { docs: { activeGroup: 0, groups: [] } } };
        var live = SH_liveShell(ref);
        // Registration happens now, against the empty state...
        var seen = null;
        var verb = function () { seen = live.docs.groups.length; };
        // ...and the shell is replaced on a later render, as React does.
        ref.current = { docs: { activeGroup: 0, groups: [{ tabs: [1, 2] }] } };
        verb();
        """
    )
    assert ctx.eval("seen") == 1, (
        "the verb read the first render's empty group list, not the current one"
    )


def test_the_live_view_reflects_every_later_replacement() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var ref2 = { current: { wid: "primer", role: "user" } };
        var live2 = SH_liveShell(ref2);
        var first = live2.wid;
        ref2.current = { wid: "ws-other", role: "admin" };
        var second = live2.wid;
        var role = live2.role;
        """
    )
    assert ctx.eval("first") == "primer"
    assert ctx.eval("second") == "ws-other"
    assert ctx.eval("role") == "admin"


def test_both_registration_sites_pass_a_live_view() -> None:
    """No verb may close over a first-render snapshot. The nv shell's
    registrations run in its own effect and close over React setters
    (stable across renders), never over derived state objects; the
    idempotency guard keeps re-renders from double-registering."""
    src = (
        ROOT / "ui" / "components" / "console" / "nv-shell.jsx"
    ).read_text(encoding="utf-8")
    assert "function reg(v) { if (!registry.get(v.id)) registry.register(v); }" \
        in src


def test_requires_live_survives_registration() -> None:
    """Regression: the gate read a field the registry had thrown away.

    register() builds an explicit `stored` object with a fixed field
    list, so anything not named there is dropped. requiresLive was not,
    so verb.requiresLive was undefined at every render and Interrupt
    stayed on the menu for sessions that had already ended. The check
    that "proved" the gate worked passed a raw verb object rather than a
    registered one, which is exactly the difference that mattered.
    """
    ctx = _ctx()
    ctx.eval(
        """
        var R = SH_createVerbRegistry();
        var stored = R.register({
          id: "session.probe", label: "Interrupt Session", weight: 1,
          surfaces: ["tab-menu"], contexts: ["session"],
          requiresLive: true, run: function () {}
        });
        var back = R.get("session.probe");
        var plain = R.register({
          id: "session.other", label: "Close Session", weight: 1,
          surfaces: ["tab-menu"], contexts: ["session"], run: function () {}
        });
        """
    )
    assert ctx.eval("stored.requiresLive") is True
    assert ctx.eval("back.requiresLive") is True, (
        "the gate reads the STORED verb, so the field has to survive get()"
    )
    assert ctx.eval("plain.requiresLive") is False
