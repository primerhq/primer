"""Task 8 of the Collection<->Workspace mount feature — the Studio file-tree
icon for mounted-collection directories.

Backend Tasks 1-7 (done) put `origin: "collection"` on mounted-collection dir
entries in the `/files/tree` response, which flows through to `item.origin` in
the Studio sidebar's tree rows. This task teaches the two pure icon helpers in
`ui/components/studio-sidebar.jsx` (`ST_fileIconName` / `ST_fileIconColor`) to
recognize that origin and render the shared "collection" glyph (already used
for collections in `ui/components/knowledge.jsx`) in the theme's `--accent`
color, instead of the generic folder ("box") icon/color a mounted dir would
otherwise get.

Both helpers are pure and JSX-free, so — per the `tests/ui` convention (see
test_studio_debug_sidebar.py's `_fn_block` slicing) — they're exercised for
real via py_mini_racer rather than only substring-matched.
"""

from __future__ import annotations

from pathlib import Path

from py_mini_racer import MiniRacer

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SIDEBAR = UI / "components" / "studio-sidebar.jsx"


def _src() -> str:
    return SIDEBAR.read_text(encoding="utf-8")


def _fn(src: str, start: str, end: str) -> str:
    """Slice `src` from `start` up to (not including) `end`."""
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def _icon_name_fn_src() -> str:
    return _fn(_src(), "function ST_fileIconName(", "function ST_fileIconColor(")


def _icon_color_fn_src() -> str:
    return _fn(_src(), "function ST_fileIconColor(", "function ST_onRowKey(")


# ---------------------------------------------------------------------------
# ST_fileIconName — mounted-collection dirs get the "collection" glyph,
# ahead of the generic is_dir -> "box" branch.
# ---------------------------------------------------------------------------


def test_icon_name_collection_origin() -> None:
    ctx = MiniRacer()
    ctx.eval("var window={};")
    ctx.eval(_icon_name_fn_src())
    ctx.eval('var a = ST_fileIconName({is_dir:true, origin:"collection", name:"slo"});')
    ctx.eval('var b = ST_fileIconName({is_dir:true, name:"other"});')
    assert ctx.eval("a") == "collection"
    assert ctx.eval("b") == "box"


def test_icon_name_collection_origin_checked_before_is_dir_branch() -> None:
    # The Controller note requires this check to come FIRST, ahead of the
    # `is_dir` -> "box" line, so a mounted dir never falls through to "box".
    fn = _icon_name_fn_src()
    origin_idx = fn.index('if (item.origin === "collection") return "collection";')
    is_dir_idx = fn.index('if (item.is_dir) return "box";')
    assert origin_idx < is_dir_idx


# ---------------------------------------------------------------------------
# ST_fileIconColor — mounted-collection dirs render in var(--accent), the
# same theme var knowledge.jsx already uses for the collection glyph.
# ---------------------------------------------------------------------------


def test_icon_color_collection_origin() -> None:
    ctx = MiniRacer()
    ctx.eval("var window={};")
    ctx.eval(_icon_color_fn_src())
    ctx.eval('var a = ST_fileIconColor({is_dir:true, origin:"collection", name:"slo"});')
    ctx.eval('var b = ST_fileIconColor({is_dir:true, name:"other"});')
    assert ctx.eval("a") == "var(--accent)"
    assert ctx.eval("b") == "var(--text-3)"


def test_icon_color_collection_origin_checked_before_is_dir_branch() -> None:
    fn = _icon_color_fn_src()
    origin_idx = fn.index('if (item.origin === "collection") return "var(--accent)";')
    is_dir_idx = fn.index('if (item.is_dir) return "var(--text-3)";')
    assert origin_idx < is_dir_idx


# ---------------------------------------------------------------------------
# Bundle transpile gate (whole bundle must still parse cleanly).
# ---------------------------------------------------------------------------


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
