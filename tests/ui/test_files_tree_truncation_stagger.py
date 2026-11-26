"""Console review 8.4, UI half: the capped files tree says how much it shows, and N expanded
directories do not all re-list on the same tick.

The server (lane I2) caps one directory listing at 5000 entries and answers ``truncated`` /
``total``; the tree shows "Showing N of M" instead of implying completeness. The root keeps
the exact C-038 cadence; a subtree's poll period is the base plus a deterministic path-derived
offset, so its ticks land between the root's. ``.nv-file-row`` skips offscreen layout work the
way ``.st-file-row`` already does.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
FILES = (UI / "components" / "console" / "nv-files-sidebar.jsx").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start) + len("\n}\n")]


@pytest.fixture
def ctx() -> Iterator[object]:
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval("var window = globalThis;")
    c.eval(_function(FILES, "NV_filesStaggerMs"))
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def test_the_stagger_stays_within_one_extra_cadence(ctx) -> None:
    for path in (".", "a", "a/b", "node_modules", "src/components", "data/2026-11"):
        for base in (5000, 15000):
            v = js(ctx, "NV_filesStaggerMs('w1', " + json.dumps(path) + ", " + str(base) + ")")
            assert base <= v < base * 2, (path, base, v)


def test_the_stagger_is_deterministic_and_spreads_the_paths(ctx) -> None:
    a1 = js(ctx, "NV_filesStaggerMs('w1', 'x/y', 5000)")
    a2 = js(ctx, "NV_filesStaggerMs('w1', 'x/y', 5000)")
    assert a1 == a2
    offsets = [
        js(ctx, "NV_filesStaggerMs('w1', " + json.dumps(p) + ", 5000)") - 5000
        for p in ("a", "b", "c", "d", "e", "f")
    ]
    assert len(set(offsets)) > 1, "six sibling folders must not all share one tick"


def test_a_zero_cadence_stays_zero(ctx) -> None:
    assert js(ctx, "NV_filesStaggerMs('w1', 'x', 0)") == 0


def test_the_subtree_poll_is_staggered_and_the_root_keeps_the_exact_cadence() -> None:
    subtree = _function(FILES, "NV_FilesSubtree")
    assert "pollMs: NV_filesStaggerMs(con.wid, props.path, filesPollMs)" in subtree
    root = FILES[FILES.index("function NV_FilesSidebar("):]
    assert "pollMs: filesPollMs, deps: [con.wid]" in root
    assert "window.NV_filesStaggerMs = NV_filesStaggerMs;" in FILES


def test_a_truncated_listing_says_showing_n_of_total() -> None:
    note = _function(FILES, "NV_filesTruncatedNote")
    assert "Showing {((data.items || []).length)} of {data.total} entries here." in note
    assert "data.truncated" in note
    # Both listing sites render it: the root and every expanded subtree.
    assert FILES.count('NV_filesTruncatedNote(tree.data, "nv-files-truncated")') == 2


def test_files_rows_skip_offscreen_layout_like_the_studio_rows() -> None:
    rule = CSS[CSS.index(".nv-file-row {"):CSS.index("}", CSS.index(".nv-file-row {"))]
    assert "content-visibility: auto" in rule
    assert "contain-intrinsic-size" in rule


def test_the_phone_files_tree_staggers_and_says_when_it_is_cut() -> None:
    """The mobile Files tab is the same capped endpoint (nv-mobile-shell.jsx keeps a local
    subtree copy): it must not re-list every expanded directory on the same 5 s tick either,
    and a capped listing must say so on the phone as well as the desktop."""
    MOB = (UI / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
    subtree = MOB[MOB.index("function NV_Mobile_FilesSubtree("):]
    subtree = subtree[:subtree.index("\n}\n")]
    assert "NV_filesStaggerMs(props.wid, props.path, 5000)" in subtree
    assert 'NV_filesTruncatedNote(tree.data, "nv-mob-files-truncated")' in subtree
    # The root listing gets the note too (its items.map is the only one with depth 0).
    root = MOB[MOB.index('data-testid="nv-mob-files-tree"'):]
    root = root[:root.index("<window.BottomSheet")]
    assert "NV_filesTruncatedNote(tree.data, \"nv-mob-files-truncated\")" in root
    # The note is one component, exported for the phone to reuse.
    assert "window.NV_filesTruncatedNote = NV_filesTruncatedNote;" in FILES
