"""Tasks 8-9 of the Collection<->Workspace mount feature, in the Studio Files
sidebar (`ui/components/studio-sidebar.jsx`).

Task 8 (icon): Backend Tasks 1-7 (done) put `origin: "collection"` on
mounted-collection dir entries in the `/files/tree` response, which flows
through to `item.origin` in the Studio sidebar's tree rows. That task taught
the two pure icon helpers (`ST_fileIconName` / `ST_fileIconColor`) to
recognize that origin and render the shared "collection" glyph (already used
for collections in `ui/components/knowledge.jsx`) in the theme's `--accent`
color, instead of the generic folder ("box") icon/color a mounted dir would
otherwise get.

Both helpers are pure and JSX-free, so — per the `tests/ui` convention (see
test_studio_debug_sidebar.py's `_fn_block` slicing) — they're exercised for
real via py_mini_racer rather than only substring-matched.

Task 9 (mount modal + header action): adds a "Mount collection" button to the
Files-header action group (`data-testid="files-mount-collection"`) and the
`ST_MountCollectionModal` component it opens — a collection picker (GET
/collections?limit=200) + optional dest name, POSTing to
`/workspaces/{wid}/mounts` and refreshing the tree (`onMounted={handleRefresh}`)
on success. `ST_MountCollectionModal` is JSX (not pure), so — like the file's
other dialogs (`ST_SessionRenameDialog` etc.) — it's exercised with
source-string asserts plus the whole-bundle transpile gate, not py_mini_racer.
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
# Task 9 — "Mount collection" header action + ST_MountCollectionModal.
# Source-string asserts (the component is JSX, not a pure/JSX-free helper).
# ---------------------------------------------------------------------------


def _mount_modal_fn_src() -> str:
    return _fn(_src(), "function ST_MountCollectionModal(", "// ---------------------------------------------------------------------------\n// FilesTree")


def test_mount_action_present_and_posts() -> None:
    src = _src()
    assert 'data-testid="files-mount-collection"' in src
    assert '"/workspaces/" + wid + "/mounts"' in src or '/mounts"' in src
    assert "ST_MountCollectionModal" in src
    assert "/collections?limit=200" in src


def test_mount_header_button_in_files_header_group() -> None:
    # The button must live inside the Files-header action group (alongside
    # New file / Upload / New folder / Refresh), not just anywhere in the
    # file. Slice from the header's data-testid to the Refresh button's
    # title as a nearby, stable anchor.
    src = _src()
    header_src = _fn(src, 'data-testid="files-header"', 'title="Refresh"')
    assert 'data-testid="files-mount-collection"' in header_src
    assert "setMountOpen(true)" in header_src
    assert "e.stopPropagation();" in header_src


def test_mount_open_state_declared() -> None:
    assert "var [mountOpen, setMountOpen] = React.useState(false);" in _src()


def test_mount_modal_rendered_with_refresh_wiring() -> None:
    src = _src()
    assert "<ST_MountCollectionModal" in src
    assert "onMounted={handleRefresh}" in src


def test_mount_collection_picker_and_dest_input_present() -> None:
    fn = _mount_modal_fn_src()
    assert 'data-testid="mount-collection-select"' in fn
    assert "items.map(function (c)" in fn
    assert "collection_id" in fn
    assert "dest" in fn


def test_mount_submit_button_testid_and_disabled_guard() -> None:
    fn = _mount_modal_fn_src()
    assert 'data-testid="mount-collection-submit"' in fn
    assert "disabled={!collectionId || create.loading}" in fn


def test_mount_posts_to_mounts_endpoint_with_encoded_wid() -> None:
    fn = _mount_modal_fn_src()
    assert '"/workspaces/" + encodeURIComponent(wid) + "/mounts"' in fn


def test_mount_success_closes_toasts_and_refreshes() -> None:
    # onSuccess must close the modal, fire a success toast, AND call
    # onMounted() so FilesTree's handleRefresh reloads the tree — a mount
    # that "succeeds" but leaves a stale tree would be a silent regression.
    fn = _mount_modal_fn_src()
    onsuccess = _fn(fn, "onSuccess: function (row) {", "onError: function (err) {")
    assert "onClose();" in onsuccess
    assert "pushToast &&" in onsuccess
    assert "onMounted && onMounted();" in onsuccess


def test_mount_error_surfaces_toast() -> None:
    fn = _mount_modal_fn_src()
    onerror = _fn(fn, "onError: function (err) {", "});")
    assert "pushToast &&" in onerror
    assert "Mount failed" in onerror


# ---------------------------------------------------------------------------
# Bundle transpile gate (whole bundle must still parse cleanly).
# ---------------------------------------------------------------------------


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
