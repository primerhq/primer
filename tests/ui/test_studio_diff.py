"""Studio revamp: line diffing (ui/studio/STUDIO-WIRING.md §7).

The diff is hand-rolled (the console ships no bundler, so the alternative is
vendoring another UMD blob), which means the LCS walk and both size guards have
to be exercised for real rather than eyeballed. Every test here runs the actual
JavaScript in MiniRacer.

The Changes VIEW that consumes this is still blocked on a backend addition (the
plan's §0.2: CommitInfo carries no file list and there is no diff route), but
the diffing itself needs none of that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DIFF = UI / "components" / "studio" / "st-diff.jsx"


def _ctx():
    """MiniRacer with st-diff's pure logic loaded (no React)."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    src = DIFF.read_text(encoding="utf-8")
    ctx.eval(src[: src.index("// ---------------------------------------------------------------------------\n// Rendering")])
    return ctx


def _diff(ctx, before: str, after: str):
    ctx.eval(
        "var out = ST2_diffLines(%s, %s);" % (json.dumps(before), json.dumps(after))
    )
    return json.loads(ctx.eval("JSON.stringify(out)"))


# ---------------------------------------------------------------------------
# Line splitting
# ---------------------------------------------------------------------------


def test_a_trailing_newline_is_not_a_phantom_last_line() -> None:
    # Otherwise every whole-file diff reports a spurious final empty line.
    ctx = _ctx()
    ctx.eval('var n = ST2_splitLines("a\\nb\\n").length;')
    assert ctx.eval("n") == 2


def test_empty_text_has_no_lines() -> None:
    ctx = _ctx()
    ctx.eval('var a = ST2_splitLines("").length, b = ST2_splitLines(null).length;')
    assert ctx.eval("a") == 0
    assert ctx.eval("b") == 0


def test_interior_blank_lines_are_preserved() -> None:
    ctx = _ctx()
    ctx.eval('var n = ST2_splitLines("a\\n\\nb").length;')
    assert ctx.eval("n") == 3


# ---------------------------------------------------------------------------
# The diff itself
# ---------------------------------------------------------------------------


def test_identical_files_have_no_changes() -> None:
    out = _diff(_ctx(), "a\nb\nc\n", "a\nb\nc\n")
    assert out["stats"] == {"added": 0, "removed": 0}
    assert [r["kind"] for r in out["rows"]] == ["same", "same", "same"]


def test_a_single_inserted_line() -> None:
    out = _diff(_ctx(), "a\nc\n", "a\nb\nc\n")
    assert out["stats"] == {"added": 1, "removed": 0}
    kinds = [r["kind"] for r in out["rows"]]
    assert kinds == ["same", "add", "same"]
    added = [r for r in out["rows"] if r["kind"] == "add"][0]
    assert added["text"] == "b"
    # Line numbers: absent on the side where the line does not exist.
    assert added["a"] is None
    assert added["b"] == 2


def test_a_single_deleted_line() -> None:
    out = _diff(_ctx(), "a\nb\nc\n", "a\nc\n")
    assert out["stats"] == {"added": 0, "removed": 1}
    assert [r["kind"] for r in out["rows"]] == ["same", "del", "same"]
    removed = [r for r in out["rows"] if r["kind"] == "del"][0]
    assert removed["a"] == 2
    assert removed["b"] is None


def test_a_changed_line_reads_as_delete_then_add() -> None:
    out = _diff(_ctx(), "a\nb\nc\n", "a\nB\nc\n")
    assert [r["kind"] for r in out["rows"]] == ["same", "del", "add", "same"]
    assert out["stats"] == {"added": 1, "removed": 1}


def test_line_numbers_stay_correct_after_an_insertion() -> None:
    # The two gutters diverge after any add/del; an off-by-one here sends
    # "open at line N" to the wrong line.
    out = _diff(_ctx(), "a\nb\n", "a\nx\ny\nb\n")
    last = out["rows"][-1]
    assert last["kind"] == "same"
    assert last["a"] == 2
    assert last["b"] == 4


def test_creating_a_file_is_all_additions() -> None:
    out = _diff(_ctx(), "", "a\nb\n")
    assert out["stats"] == {"added": 2, "removed": 0}
    assert all(r["kind"] == "add" for r in out["rows"])


def test_emptying_a_file_is_all_deletions() -> None:
    out = _diff(_ctx(), "a\nb\n", "")
    assert out["stats"] == {"added": 0, "removed": 2}
    assert all(r["kind"] == "del" for r in out["rows"])


def test_a_move_is_reported_as_one_delete_and_one_add() -> None:
    out = _diff(_ctx(), "a\nb\nc\n", "b\nc\na\n")
    assert out["stats"] == {"added": 1, "removed": 1}


def test_lcs_finds_the_minimal_edit_not_a_wholesale_replace() -> None:
    # A naive line-by-line compare would call all five lines changed.
    out = _diff(_ctx(), "1\n2\n3\n4\n5\n", "1\n2\nX\n4\n5\n")
    assert out["stats"] == {"added": 1, "removed": 1}
    assert sum(1 for r in out["rows"] if r["kind"] == "same") == 4


def test_repeated_lines_do_not_confuse_the_walk() -> None:
    out = _diff(_ctx(), "a\na\na\n", "a\na\n")
    assert out["stats"] == {"added": 0, "removed": 1}


# ---------------------------------------------------------------------------
# The guards - both of these hang the tab rather than degrading
# ---------------------------------------------------------------------------


def test_an_oversized_side_is_refused_not_diffed() -> None:
    ctx = _ctx()
    ctx.eval("var big = 'x\\n'.repeat(200000);")
    ctx.eval("var out = ST2_diffLines(big, 'y');")
    assert ctx.eval("out.tooLarge") is True
    assert "too large" in ctx.eval("out.reason")


def test_the_cap_is_stated_in_kb_the_user_can_understand() -> None:
    ctx = _ctx()
    ctx.eval("var out = ST2_diffLines('x'.repeat(300000), 'y');")
    assert "200 KB" in ctx.eval("out.reason")


def test_a_file_just_under_the_cap_still_diffs() -> None:
    ctx = _ctx()
    ctx.eval("var s = 'a\\n'.repeat(1000);")
    ctx.eval("var out = ST2_diffLines(s, s + 'b\\n');")
    assert ctx.eval("!!out.tooLarge") is False
    assert ctx.eval("out.stats.added") == 1


def test_a_wholesale_rewrite_degrades_instead_of_freezing() -> None:
    # Nothing trims, so the DP would be huge. It must fall back, stay exact on
    # the counts, and say that it did.
    ctx = _ctx()
    ctx.eval("var a = [], b = [];")
    ctx.eval("for (var i = 0; i < 3000; i++) { a.push('a' + i); b.push('b' + i); }")
    ctx.eval("var out = ST2_diffLines(a.join('\\n'), b.join('\\n'));")
    assert ctx.eval("out.coarse") is True
    assert ctx.eval("out.stats.removed") == 3000
    assert ctx.eval("out.stats.added") == 3000


def test_the_coarse_fallback_keeps_the_untouched_tail() -> None:
    # The trimmed suffix must survive the fallback path too.
    ctx = _ctx()
    ctx.eval("var a = [], b = [];")
    ctx.eval("for (var i = 0; i < 2500; i++) { a.push('a' + i); b.push('b' + i); }")
    ctx.eval("a.push('shared-tail'); b.push('shared-tail');")
    ctx.eval("var out = ST2_diffLines(a.join('\\n'), b.join('\\n'));")
    ctx.eval("var tail = out.rows[out.rows.length - 1];")
    assert ctx.eval("out.coarse") is True
    assert ctx.eval("tail.kind") == "same"
    assert ctx.eval("tail.text") == "shared-tail"


def test_a_large_but_localised_edit_is_not_coarse() -> None:
    # Prefix/suffix trimming is what keeps the common case exact.
    ctx = _ctx()
    ctx.eval("var lines = [];")
    ctx.eval("for (var i = 0; i < 5000; i++) lines.push('line ' + i);")
    ctx.eval("var a = lines.join('\\n');")
    ctx.eval("lines[2500] = 'CHANGED'; var b = lines.join('\\n');")
    ctx.eval("var out = ST2_diffLines(a, b);")
    assert ctx.eval("!!out.coarse") is False
    assert ctx.eval("out.stats.added") == 1
    assert ctx.eval("out.stats.removed") == 1


# ---------------------------------------------------------------------------
# Context collapsing
# ---------------------------------------------------------------------------


def test_long_unchanged_runs_collapse_to_a_gap_marker() -> None:
    ctx = _ctx()
    ctx.eval("var lines = [];")
    ctx.eval("for (var i = 0; i < 200; i++) lines.push('l' + i);")
    ctx.eval("var a = lines.join('\\n');")
    ctx.eval("lines[100] = 'X'; var b = lines.join('\\n');")
    ctx.eval("var rows = ST2_collapseContext(ST2_diffLines(a, b).rows, 3);")
    ctx.eval("var gaps = rows.filter(function (r) { return r.kind === 'gap'; });")
    # Two gaps: before and after the change.
    assert ctx.eval("gaps.length") == 2
    # And the whole thing is now short enough to read.
    assert ctx.eval("rows.length") < 15


def test_the_gap_marker_counts_the_lines_it_hides() -> None:
    ctx = _ctx()
    ctx.eval("var lines = [];")
    ctx.eval("for (var i = 0; i < 50; i++) lines.push('l' + i);")
    ctx.eval("var a = lines.join('\\n');")
    ctx.eval("lines[49] = 'X'; var b = lines.join('\\n');")
    ctx.eval("var rows = ST2_collapseContext(ST2_diffLines(a, b).rows, 3);")
    ctx.eval("var gap = rows.filter(function (r) { return r.kind === 'gap'; })[0];")
    # 50 lines, last one changed, 3 lines of context kept -> 46 hidden.
    assert ctx.eval("gap.count") == 46


def test_changed_lines_are_never_collapsed() -> None:
    ctx = _ctx()
    ctx.eval("var rows = ST2_collapseContext(ST2_diffLines('a\\nb\\n', 'a\\nB\\n').rows, 3);")
    ctx.eval("var kinds = rows.map(function (r) { return r.kind; }).join(',');")
    assert "del" in ctx.eval("kinds")
    assert "add" in ctx.eval("kinds")
    assert "gap" not in ctx.eval("kinds")


def test_an_unchanged_file_collapses_entirely() -> None:
    ctx = _ctx()
    ctx.eval("var rows = ST2_collapseContext(ST2_diffLines('a\\nb\\nc\\n', 'a\\nb\\nc\\n').rows, 3);")
    ctx.eval("var kinds = rows.map(function (r) { return r.kind; }).join(',');")
    assert ctx.eval("kinds") == "gap"


# ---------------------------------------------------------------------------
# No vendored library, and it renders
# ---------------------------------------------------------------------------


def test_no_diff_library_was_vendored() -> None:
    names = [p.name.lower() for p in (UI / "vendor").iterdir()]
    for lib in ("diff", "jsdiff", "diff2html", "diff-match-patch"):
        assert not any(lib in n for n in names), lib


def test_diff_view_reports_both_counts_and_the_refusal() -> None:
    src = DIFF.read_text(encoding="utf-8")
    for tid in ("diff-view", "diff-added", "diff-removed", "diff-too-large",
                "diff-gap", "diff-coarse"):
        assert f'"{tid}"' in src, tid
    assert '"diff-row-" + row.kind' in src


def test_diff_module_transpiles_and_is_registered() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    assert b._transform(DIFF.read_text(encoding="utf-8"), "components/studio/st-diff.jsx")
    assert "components/studio/st-diff.jsx" in (UI / "index.html").read_text(encoding="utf-8")
