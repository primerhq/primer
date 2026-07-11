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
    # onMounted must refresh BOTH the file tree (handleRefresh -> rootRes)
    # AND the shared studio-mounts resource (mountsRes.refetch) — otherwise
    # a collection mounted in the current session is missing from
    # mountsByDest, so its right-click "Detach collection" no-ops
    # (handleDetach's `if (!mount) return;`) and its dirty-dot can't show
    # until a page reload. Anchor the assertion to the modal's render.
    src = _src()
    assert "<ST_MountCollectionModal" in src
    modal_render = _fn(src, "<ST_MountCollectionModal", "/>")
    assert "handleRefresh();" in modal_render
    assert "mountsRes.refetch" in modal_render
    # The bare `onMounted={handleRefresh}` (tree-only) form must be gone.
    assert "onMounted={handleRefresh}" not in src


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
# Task 11 — Detach a mounted collection + dirty-dot indicator.
#
# SCOPE (Controller note, binding): Detach + dirty-dot + the shared
# `studio-mounts` resource ONLY. The "apply to collection" menu entry and
# `handleApplyToCollection` are Task 12 and must NOT appear yet — see
# test_apply_collection_not_yet_added below, which guards against a
# dangling reference to a not-yet-implemented handler.
# ---------------------------------------------------------------------------


def _detach_fn_src() -> str:
    return _fn(_src(), "async function handleDetach(", "function ST_readAsBase64(")


def test_detach_menu_entry_gated_on_collection_origin() -> None:
    src = _src()
    assert 'menu.item && menu.item.origin === "collection"' in src
    assert '{ key: "detach"' in src or 'key:"detach"' in src


def test_detach_dispatch_wired() -> None:
    src = _src()
    assert 'else if (action === "detach") handleDetach(item);' in src


def test_handle_detach_looks_up_mount_and_calls_endpoint() -> None:
    fn = _detach_fn_src()
    assert "mountsByDest[item.path]" in fn
    assert '"/mounts/"' in fn
    assert "mount.mount_id" in fn


def test_handle_detach_409_confirms_then_retries_with_force() -> None:
    fn = _detach_fn_src()
    assert "err.status === 409" in fn
    assert "confirmDialog(" in fn
    assert "force=true" in fn


def test_handle_detach_success_refreshes_tree_and_mounts() -> None:
    fn = _detach_fn_src()
    assert "handleRefresh();" in fn
    assert "mountsRes.refetch" in fn
    assert "pushToast &&" in fn


def test_shared_mounts_resource_declared() -> None:
    src = _src()
    assert '"studio-mounts:" + wid' in src
    assert "mountsByDest" in src
    assert "/mounts\"" in src or '"/mounts"' in src


def test_collection_dirty_dot_rendered() -> None:
    src = _src()
    assert 'data-testid="collection-dirty-dot"' in src
    assert "var(--warn, #d9822b)" in src
    assert (
        'item.origin === "collection" && mountsByDest[item.path] && '
        "mountsByDest[item.path].dirty" in src
    )



# ---------------------------------------------------------------------------
# Task 12 — "Apply to collection" diff-preview modal.
#
# Adds the "apply-collection" context-menu entry (before "detach", both
# gated on origin==="collection"), the `applyMount` state + `handleCtxAction`
# dispatch that resolves it via `mountsByDest`, and the `ST_ApplyPreviewModal`
# component itself (GET .../diff, POST .../apply). Source-string asserts, like
# the file's other dialogs (ST_MountCollectionModal etc.), plus the whole-
# bundle transpile gate.
# ---------------------------------------------------------------------------


def _apply_modal_fn_src() -> str:
    return _fn(
        _src(),
        "function ST_ApplyPreviewModal(",
        "// ---------------------------------------------------------------------------\n// FilesTree",
    )


def test_apply_menu_entry_gated_on_collection_origin_before_detach() -> None:
    src = _src()
    assert '{ key: "apply-collection", label: "Apply to collection", icon: "upload", danger: false }' in src
    # Both entries are appended in the same `origin === "collection"` block;
    # apply must come before detach in that block.
    origin_block = _fn(src, 'menu.item && menu.item.origin === "collection"', '{ key: "detach"')
    assert "apply-collection" in origin_block


def test_apply_state_declared() -> None:
    assert "var [applyMount, setApplyMount] = React.useState(null);" in _src()


def test_apply_dispatch_wired() -> None:
    src = _src()
    assert (
        'else if (action === "apply-collection") { var m = mountsByDest[item.path]; if (m) setApplyMount(m); }'
        in src
    )


def test_apply_modal_rendered_with_refresh_wiring() -> None:
    src = _src()
    assert "<ST_ApplyPreviewModal" in src
    render = _fn(src, "<ST_ApplyPreviewModal", "/>")
    assert "handleRefresh();" in render
    assert "mountsRes.refetch" in render
    assert "onClose={function () { setApplyMount(null); }}" in render


def test_apply_modal_fetches_diff_and_posts_apply() -> None:
    fn = _apply_modal_fn_src()
    assert '"/mounts/" + encodeURIComponent(mount.mount_id) + "/diff"' in fn
    assert '"/mounts/" + encodeURIComponent(mount.mount_id) + "/apply"' in fn
    assert "api.useResource(" in fn
    assert "api.useMutation(" in fn


def test_apply_modal_groups_and_conflicts() -> None:
    # The Added/Modified/Deleted lists share one `group(label, arr, testid)`
    # helper, so their testids are passed as call args, not literal
    # `data-testid="..."` JSX attrs (that literal form is only used for the
    # dynamic-free conflicts/orphaned/submit testids).
    fn = _apply_modal_fn_src()
    assert '"apply-added"' in fn
    assert '"apply-modified"' in fn
    assert '"apply-deleted"' in fn
    assert 'data-testid="apply-conflicts"' in fn
    assert "var(--warn, #d9822b)" in fn
    assert 'data-testid="apply-orphaned"' in fn


def test_apply_modal_submit_disabled_guard_and_testid() -> None:
    fn = _apply_modal_fn_src()
    assert 'data-testid="apply-collection-submit"' in fn
    assert "disabled={diffRes.loading || apply.loading || d.orphaned || total === 0}" in fn


def test_apply_modal_success_toasts_applied_count_and_refreshes() -> None:
    fn = _apply_modal_fn_src()
    onsuccess = _fn(fn, "onSuccess: function () {", "onError: function (err) {")
    assert "onClose();" in onsuccess
    assert '"Applied " + total' in onsuccess
    assert "onApplied && onApplied();" in onsuccess


def test_apply_modal_error_surfaces_toast() -> None:
    fn = _apply_modal_fn_src()
    onerror = _fn(fn, "onError: function (err) {", "});")
    assert "pushToast &&" in onerror
    assert "Apply failed" in onerror


def test_apply_modal_exported_on_window() -> None:
    assert "window.ST_ApplyPreviewModal = ST_ApplyPreviewModal;" in _src()


# ---------------------------------------------------------------------------
# Bundle transpile gate (whole bundle must still parse cleanly).
# ---------------------------------------------------------------------------


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
