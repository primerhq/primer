"""Studio revamp: the Changes view (ui/studio/STUDIO-WIRING.md §7).

Two things carry the risk here and both run for real in MiniRacer:

1. The unified-diff parser. It renumbers every line from the hunk headers, so an
   off-by-one silently mislabels which line changed - and a diff that points at
   the wrong line is worse than no diff.
2. The trail grouping, where `files: null` (a backend that cannot report file
   data) must never be counted as zero.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DIFF = UI / "components" / "studio" / "st-diff.jsx"
CHANGES = UI / "components" / "studio" / "st-changes.jsx"
API = UI / "components" / "studio" / "st-api.jsx"
CENTER = UI / "components" / "studio-center.jsx"
STUDIO = UI / "components" / "studio.jsx"


def _code_only(src: str) -> str:
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def _ctx():
    """MiniRacer with the pure diff + grouping logic (no React)."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    d = DIFF.read_text(encoding="utf-8")
    ctx.eval(d[: d.index("// ---------------------------------------------------------------------------\n// Rendering")])
    # ST2_pairRows lives in the rendering half but is pure, so lift just it.
    ctx.eval(d[d.index("function ST2_pairRows(rows)"): d.index("function ST2_SideCell(")])
    c = CHANGES.read_text(encoding="utf-8")
    ctx.eval(c[c.index("function ST2_opTone(op)"): c.index("// ------", c.index("function ST2_groupTrail("))])
    # ST2_bucketOf, used by the grouping.
    s = (UI / "components" / "studio" / "st-status.jsx").read_text(encoding="utf-8")
    ctx.eval(s[: s.index("window.")])
    return ctx


def _parse(ctx, patch: str):
    ctx.eval(f"var out = ST2_parseUnifiedDiff({json.dumps(patch)});")
    return json.loads(ctx.eval("JSON.stringify(out)"))


# A real `git show` patch body for one file.
PATCH = """diff --git a/f1.txt b/f1.txt
index 0123456..789abcd 100644
--- a/f1.txt
+++ b/f1.txt
@@ -1,3 +1,4 @@
 a
-b
+B
 c
+d
"""


# ---------------------------------------------------------------------------
# The unified-diff parser
# ---------------------------------------------------------------------------


def test_the_preamble_is_not_rendered_as_content() -> None:
    # diff --git / index / --- / +++ lines start with characters the row parser
    # also uses ('-', '+'), so a parser that starts before the first @@ emits
    # the file header as though it were removed and added lines.
    out = _parse(_ctx(), PATCH)
    texts = [r["text"] for r in out["rows"]]
    assert not any("diff --git" in t for t in texts)
    assert not any(t.startswith("a/f1.txt") or t.startswith("b/f1.txt") for t in texts)
    assert out["stats"] == {"added": 2, "removed": 1}


def test_rows_come_out_in_order_with_the_right_kinds() -> None:
    out = _parse(_ctx(), PATCH)
    assert [r["kind"] for r in out["rows"]] == ["same", "del", "add", "same", "add"]
    assert [r["text"] for r in out["rows"]] == ["a", "b", "B", "c", "d"]


def test_line_numbers_are_taken_from_the_hunk_header() -> None:
    # A diff that points at the wrong line is worse than no diff.
    out = _parse(_ctx(), PATCH)
    rows = out["rows"]
    assert (rows[0]["a"], rows[0]["b"]) == (1, 1)   # context 'a'
    assert (rows[1]["a"], rows[1]["b"]) == (2, None)  # removed 'b'
    assert (rows[2]["a"], rows[2]["b"]) == (None, 2)  # added 'B'
    assert (rows[3]["a"], rows[3]["b"]) == (3, 3)   # context 'c'
    assert (rows[4]["a"], rows[4]["b"]) == (None, 4)  # added 'd'


def test_a_hunk_starting_late_in_the_file_numbers_from_its_offset() -> None:
    out = _parse(_ctx(), (
        "--- a/f\n+++ b/f\n"
        "@@ -120,3 +120,3 @@\n x\n-y\n+Y\n z\n"
    ))
    rows = out["rows"]
    assert rows[0]["a"] == 120
    assert rows[1]["a"] == 121
    assert rows[2]["b"] == 121
    assert rows[3]["a"] == 122


def test_multiple_hunks_get_a_gap_between_them() -> None:
    out = _parse(_ctx(), (
        "--- a/f\n+++ b/f\n"
        "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        "@@ -50,2 +50,2 @@\n y\n-z\n+Z\n"
    ))
    kinds = [r["kind"] for r in out["rows"]]
    assert "gap" in kinds
    assert kinds.index("gap") > 0, "no leading gap before the first hunk"
    # The skipped run's length is genuinely unknown - git never sent it.
    gap = next(r for r in out["rows"] if r["kind"] == "gap")
    assert gap["count"] is None
    assert out["stats"] == {"added": 2, "removed": 2}


def test_the_second_hunk_numbers_from_its_own_header() -> None:
    out = _parse(_ctx(), (
        "--- a/f\n+++ b/f\n"
        "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        "@@ -50,2 +50,2 @@\n y\n-z\n+Z\n"
    ))
    tail = [r for r in out["rows"] if r["kind"] != "gap"][-3:]
    assert tail[0]["a"] == 50   # context 'y'
    assert tail[1]["a"] == 51   # removed 'z'
    assert tail[2]["b"] == 51   # added 'Z'


def test_a_single_line_hunk_header_without_a_count_parses() -> None:
    # git writes "@@ -1 +1 @@" when the range is one line.
    out = _parse(_ctx(), "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+A\n")
    assert out["stats"] == {"added": 1, "removed": 1}
    assert out["rows"][0]["a"] == 1
    assert out["rows"][1]["b"] == 1


def test_no_newline_marker_is_not_content() -> None:
    out = _parse(_ctx(), "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+A\n")
    assert [r["text"] for r in out["rows"]] == ["a", "A"]
    assert out["stats"] == {"added": 1, "removed": 1}


def test_an_empty_context_line_is_kept_as_a_line() -> None:
    # git emits a bare "" for a blank context line, not " ".
    out = _parse(_ctx(), "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n\n-c\n+C\n")
    kinds = [r["kind"] for r in out["rows"]]
    assert kinds == ["same", "same", "del", "add"]


def test_a_binary_patch_is_reported_not_parsed_as_lines() -> None:
    out = _parse(_ctx(), "diff --git a/b.png b/b.png\nBinary files a/b.png and b/b.png differ\n")
    assert out["binary"] is True
    assert out["rows"] == []


def test_an_empty_patch_yields_nothing() -> None:
    ctx = _ctx()
    for value in ('""', "null", "undefined"):
        ctx.eval(f"var o = ST2_parseUnifiedDiff({value});")
        assert ctx.eval("o.rows.length") == 0
        assert ctx.eval("o.stats.added") == 0


def test_a_pure_addition_patch_counts_every_line() -> None:
    out = _parse(_ctx(), "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,3 @@\n+a\n+b\n+c\n")
    assert out["stats"] == {"added": 3, "removed": 0}
    assert [r["b"] for r in out["rows"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Side-by-side pairing
# ---------------------------------------------------------------------------


def test_a_changed_line_pairs_its_removal_with_its_replacement() -> None:
    ctx = _ctx()
    ctx.eval("""
        var rows = [
          {kind: "same", a: 1, b: 1, text: "a"},
          {kind: "del", a: 2, b: null, text: "b"},
          {kind: "add", a: null, b: 2, text: "B"}
        ];
        var pairs = ST2_pairRows(rows);
    """)
    assert ctx.eval("pairs.length") == 2
    assert ctx.eval("pairs[1].left.text") == "b"
    assert ctx.eval("pairs[1].right.text") == "B"


def test_an_uneven_del_add_run_leaves_the_short_side_blank() -> None:
    ctx = _ctx()
    ctx.eval("""
        var rows = [
          {kind: "del", a: 1, b: null, text: "x"},
          {kind: "del", a: 2, b: null, text: "y"},
          {kind: "add", a: null, b: 1, text: "X"}
        ];
        var pairs = ST2_pairRows(rows);
        var secondRight = pairs[1].right;
    """)
    assert ctx.eval("pairs.length") == 2
    assert ctx.eval("secondRight") is None


def test_pairing_never_drops_a_row() -> None:
    ctx = _ctx()
    ctx.eval("""
        var rows = [
          {kind: "add", a: null, b: 1, text: "1"},
          {kind: "same", a: 1, b: 2, text: "2"},
          {kind: "del", a: 2, b: null, text: "3"}
        ];
        var pairs = ST2_pairRows(rows);
        var seen = 0;
        pairs.forEach(function (p) {
          if (p.both) { seen += 1; return; }
          if (p.left) seen += 1;
          if (p.right) seen += 1;
        });
    """)
    assert ctx.eval("seen") == 3


def test_layout_is_chosen_by_width_not_by_a_user_toggle() -> None:
    src = DIFF.read_text(encoding="utf-8")
    assert "ST2_SIDE_BY_SIDE_MIN_W = 900" in src
    assert "ResizeObserver" in src
    assert 'data-layout={wide ? "side-by-side" : "unified"}' in src
    code = _code_only(src)
    # No toggle: the right answer is a property of the container.
    assert "sideBySide" not in code or "setSideBySide" not in code


def test_a_long_diff_is_capped_and_says_so() -> None:
    src = DIFF.read_text(encoding="utf-8")
    assert "ST2_DIFF_ROW_CAP = 500" in src
    assert '"diff-truncated"' in src
    assert "rows.slice(0, ST2_DIFF_ROW_CAP)" in src


def test_a_parsed_patch_is_not_re_collapsed() -> None:
    # git already omitted its unchanged runs; collapsing again would eat the
    # context lines it deliberately sent.
    src = DIFF.read_text(encoding="utf-8")
    assert "patch != null ? diff.rows : ST2_collapseContext(diff.rows, context)" in src


# ---------------------------------------------------------------------------
# Trail grouping
# ---------------------------------------------------------------------------


def _group(ctx, commits, sessions=None):
    ctx.eval(
        f"var g = ST2_groupTrail({json.dumps(commits)}, "
        f"{{sessionsById: {json.dumps(sessions or {})}}});"
    )
    return json.loads(ctx.eval("JSON.stringify(g)"))


def test_commits_group_by_session_in_first_seen_order() -> None:
    groups = _group(_ctx(), [
        {"sha": "a", "session_id": "s1", "files": [{"path": "x", "additions": 1, "deletions": 0}]},
        {"sha": "b", "session_id": "s2", "files": [{"path": "y", "additions": 1, "deletions": 0}]},
        {"sha": "c", "session_id": "s1", "files": [{"path": "z", "additions": 1, "deletions": 0}]},
    ])
    assert [g["session_id"] for g in groups] == ["s1", "s2"]
    assert len(groups[0]["commits"]) == 2


def test_a_commit_with_no_session_is_kept_not_dropped() -> None:
    # Platform writes have no session trailer; dropping them would hide real
    # changes to the workspace.
    groups = _group(_ctx(), [
        {"sha": "a", "session_id": None, "files": [{"path": "x", "additions": 1, "deletions": 0}]},
    ])
    assert len(groups) == 1
    assert groups[0]["session_id"] is None
    assert groups[0]["label"] == "workspace"


def test_null_files_is_never_counted_as_zero() -> None:
    # The sandbox backend cannot report file data. Counting null as 0 would
    # draw "0 files" over a commit that certainly touched some.
    groups = _group(_ctx(), [{"sha": "a", "session_id": "s1", "files": None}])
    assert groups[0]["fileCount"] == 0
    assert groups[0]["unknownFiles"] is True


def test_a_known_empty_commit_is_not_flagged_unknown() -> None:
    groups = _group(_ctx(), [{"sha": "a", "session_id": "s1", "files": []}])
    assert groups[0]["fileCount"] == 0
    assert groups[0]["unknownFiles"] is False


def test_file_counts_sum_across_a_sessions_commits() -> None:
    groups = _group(_ctx(), [
        {"sha": "a", "session_id": "s1", "files": [
            {"path": "x", "additions": 1, "deletions": 0},
            {"path": "y", "additions": 2, "deletions": 1},
        ]},
        {"sha": "b", "session_id": "s1", "files": [{"path": "z", "additions": 1, "deletions": 0}]},
    ])
    assert groups[0]["fileCount"] == 3


def test_the_group_label_prefers_the_session_name() -> None:
    groups = _group(
        _ctx(),
        [{"sha": "a", "session_id": "s1", "files": []}],
        {"s1": {"session_id": "s1", "name": "refactor the parser", "status": "running"}},
    )
    assert groups[0]["label"] == "refactor the parser"
    assert groups[0]["bucket"] == "working"


def test_file_tone_distinguishes_create_delete_and_edit() -> None:
    ctx = _ctx()
    ctx.eval("var created = ST2_fileTone({additions: 5, deletions: 0});")
    ctx.eval("var deleted = ST2_fileTone({additions: 0, deletions: 5});")
    ctx.eval("var edited = ST2_fileTone({additions: 5, deletions: 5});")
    ctx.eval("var bin = ST2_fileTone({binary: true, additions: 0, deletions: 0});")
    assert ctx.eval("created") == "--green"
    assert ctx.eval("deleted") == "--red"
    assert ctx.eval("edited") == "--blue"
    assert ctx.eval("bin") == "--text-3"


# ---------------------------------------------------------------------------
# Wiring + the honest gaps
# ---------------------------------------------------------------------------


def test_the_trail_uses_with_files_and_the_commit_endpoint_for_patches() -> None:
    api = API.read_text(encoding="utf-8")
    assert "with_files=1" in api
    assert '"/commit/"' in api
    changes = CHANGES.read_text(encoding="utf-8")
    assert "ST2_api.trail(wid, ST2_CHANGES_LIMIT, true, signal)" in changes
    assert "ST2_api.commit(wid, sel.commit.sha, signal)" in changes


def test_the_trail_is_not_polled() -> None:
    # History does not change under the reader; WS_LogTab's manual refresh is
    # kept deliberately (§7.1).
    src = CHANGES.read_text(encoding="utf-8")
    trail_block = src[src.index("var trail = api.useResource("):src.index("var commits =")]
    assert "pollMs" not in trail_block
    assert '"changes-refresh"' in src


def test_patches_are_fetched_per_opened_commit_only() -> None:
    # The whole reason the trail carries counts instead of content.
    src = CHANGES.read_text(encoding="utf-8")
    assert "function ST2_ChangesDetail(" in src
    detail = src[src.index("function ST2_ChangesDetail("):src.index("function ChangesView(")]
    assert "ST2_api.keys.commit(" in detail


def test_revert_stays_hidden_until_a_backend_op_exists() -> None:
    src = CHANGES.read_text(encoding="utf-8")
    assert '"studio.revert"' in src
    assert "revertEnabled ? (" in src
    # Never faked with a file write.
    code = _code_only(src)
    assert "files/write" not in code
    assert 'apiFetch("PUT"' not in code


def test_mark_reviewed_is_local_and_labelled_honestly() -> None:
    src = CHANGES.read_text(encoding="utf-8")
    assert "localStorage" in src
    assert '"changes-unreviewed-chip"' in src
    assert ">Unreviewed<" in src


def test_show_the_turn_opens_in_the_other_pane() -> None:
    # So the diff stays on screen next to its explanation (§7.2).
    src = CHANGES.read_text(encoding="utf-8")
    assert "studio.openAside" in src
    assert '"changes-show-turn"' in src


def test_no_rationale_field_is_invented() -> None:
    # The footer is the commit subject the runtime already writes.
    src = CHANGES.read_text(encoding="utf-8")
    assert '"changes-rationale"' in src
    assert "sel.commit.subject" in src
    assert "rationale:" not in _code_only(src)


def test_changes_is_a_center_tab_with_a_header_entry_point() -> None:
    center = CENTER.read_text(encoding="utf-8")
    assert 'activeTab.kind === "changes"' in center
    studio = STUDIO.read_text(encoding="utf-8")
    assert '"studio-changes-toggle"' in studio
    assert 'kind: "changes"' in studio
    # v2 only, like every other revamp surface.
    assert "onOpenChanges={isV2 ?" in studio


def test_changes_does_not_name_urls_directly() -> None:
    src = CHANGES.read_text(encoding="utf-8")
    assert "/workspaces/" not in _code_only(src)
    assert "ST2_api." in src


def test_changes_registered_after_the_diff_module() -> None:
    lines = (UI / "index.html").read_text(encoding="utf-8").splitlines()
    reg = [i for i, ln in enumerate(lines) if 'type="text/babel"' in ln and "src=" in ln]

    def idx(frag: str) -> int:
        for i in reg:
            if frag in lines[i]:
                return i
        raise AssertionError(f"{frag} is not registered")

    assert idx("studio/st-diff.jsx") < idx("studio/st-changes.jsx")
    assert idx("studio/st-changes.jsx") < idx("components/studio.jsx")


def test_changes_modules_transpile() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    for rel in ("components/studio/st-changes.jsx", "components/studio/st-diff.jsx",
                "components/studio-center.jsx", "components/studio.jsx"):
        assert b._transform((UI / rel).read_text(encoding="utf-8"), rel), rel
