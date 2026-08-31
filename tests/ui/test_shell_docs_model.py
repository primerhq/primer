"""VS Code tab semantics wholesale (spec section 8, "Rail, tabs, palette").

The rules that matter and are easy to get wrong: ONE preview tab per
group that gets reused, promotion on edit or double click, pinning that
survives close-neighbour churn, MRU cycling, and agent-driven opens that
never steal focus and never create tab creep.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-docs.js"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval((ROOT / "ui" / "foundation" / "shell-url.js").read_text(encoding="utf-8"))
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    ctx.eval("var S = SH_emptyDocState();")
    return ctx


def test_doc_ids_are_kind_scoped() -> None:
    ctx = _ctx()
    assert ctx.eval('SH_docId("file", "src/api.ts")') == "file:src/api.ts"


def test_single_click_reuses_one_italic_preview_tab() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SH_openDoc(S, {kind: "file", ref: "a.ts", preview: true});
          s = SH_openDoc(s, {kind: "file", ref: "b.ts", preview: true});
          return JSON.stringify(s.groups[0].tabs.map(function (t) {
            return [t.id, t.preview];
          }));
        })()
        """
    ))
    assert out == [["file:b.ts", True]]


def test_edit_promotes_the_preview_and_the_next_open_adds_a_tab() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SH_openDoc(S, {kind: "file", ref: "a.ts", preview: true});
          s = SH_promoteDoc(s, "file:a.ts");
          s = SH_openDoc(s, {kind: "file", ref: "b.ts", preview: true});
          return JSON.stringify(s.groups[0].tabs.map(function (t) {
            return [t.id, t.preview];
          }));
        })()
        """
    ))
    assert out == [["file:a.ts", False], ["file:b.ts", True]]


def test_agent_driven_open_lands_in_background_with_a_badge() -> None:
    """No focus theft, no tab creep from narration."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SH_openDoc(S, {kind: "session", ref: "s1", preview: false});
          s = SH_openDoc(s, {kind: "file", ref: "src/api.ts", preview: true,
                             focus: false});
          var tab = s.groups[0].tabs[1];
          return JSON.stringify([s.groups[0].activeId, tab.preview, tab.badge]);
        })()
        """
    ))
    assert out == ["session:s1", True, True]


def test_pinned_tabs_survive_the_preview_slot_and_close_neighbours() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SH_openDoc(S, {kind: "session", ref: "s1", preview: true});
          s = SH_pinDoc(s, "session:s1", true);
          s = SH_openDoc(s, {kind: "file", ref: "a.ts", preview: true});
          s = SH_openDoc(s, {kind: "file", ref: "b.ts", preview: true});
          return JSON.stringify(s.groups[0].tabs.map(function (t) { return t.id; }));
        })()
        """
    ))
    assert out == ["session:s1", "file:b.ts"]


def test_split_right_creates_a_second_group_and_comparison_stays_in_tabs() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SH_openDoc(S, {kind: "session", ref: "s1"});
          s = SH_splitRight(s);
          s = SH_openDoc(s, {kind: "diff", ref: "9c1f2ab", group: 1});
          return JSON.stringify([
            s.groups.length,
            s.groups[1].tabs.map(function (t) { return t.id; }),
            s.activeGroup
          ]);
        })()
        """
    ))
    assert out == [2, ["diff:9c1f2ab"], 1]


def test_mru_cycling_is_recency_ordered_not_positional() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SH_openDoc(S, {kind: "file", ref: "a.ts"});
          s = SH_openDoc(s, {kind: "file", ref: "b.ts"});
          s = SH_openDoc(s, {kind: "file", ref: "c.ts"});
          s = SH_openDoc(s, {kind: "file", ref: "a.ts"});
          var next = SH_cycleMru(s, 1);
          return JSON.stringify(SH_activeDoc(next).id);
        })()
        """
    ))
    assert out == "file:c.ts"


def test_closing_the_active_tab_activates_its_left_neighbour() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SH_openDoc(S, {kind: "file", ref: "a.ts"});
          s = SH_openDoc(s, {kind: "file", ref: "b.ts"});
          s = SH_closeDoc(s, "file:b.ts");
          return JSON.stringify([s.groups[0].activeId,
            s.groups[0].tabs.map(function (t) { return t.id; })]);
        })()
        """
    ))
    assert out == ["file:a.ts", ["file:a.ts"]]


def test_unknown_kinds_are_refused() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_openDoc(S, {kind: "evil", ref: "x"}).groups[0].tabs.length === 0'
    ) is True
