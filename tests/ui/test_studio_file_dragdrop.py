"""Structural checks for the Studio Files-tree context menu + drag-and-drop.

These features are UI wiring in ``ui/components/studio-sidebar.jsx`` over the
workspace file endpoints, plus the new move endpoint:

  * Right-click context menu (ST_FileContextMenu) with per-kind actions.
  * Rename via promptDialog → POST /files/move?src=&dst= → tab remap + refresh.
  * Drag-drop upload: OS files onto a folder row / tree root → base64 PUT.
  * Drag-to-move: a row onto a folder row → POST /files/move.

The bundle has no module system (flat global scope), so these are source-level
structural assertions plus a hard transpile gate — the same pattern used by
test_studio_file_actions.py.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SIDEBAR = UI / "components" / "studio-sidebar.jsx"
STUDIO = UI / "components" / "studio.jsx"


def _src() -> str:
    return SIDEBAR.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Context menu: component + per-kind action testids
# ---------------------------------------------------------------------------


def test_context_menu_component_and_testid() -> None:
    src = _src()
    assert "function ST_FileContextMenu(" in src
    assert 'data-testid="file-context-menu"' in src
    # testids are built as "ctx-" + <action key>.
    assert '"ctx-" + a.key' in src


def test_context_menu_has_all_action_keys() -> None:
    src = _src()
    # file: Open / Download / Rename / Delete ; folder: New file here /
    # New folder here / Upload here / Rename / Delete.
    for key in (
        'key: "open"',
        'key: "download"',
        'key: "rename"',
        'key: "delete"',
        'key: "new-file"',
        'key: "new-folder"',
        'key: "upload"',
    ):
        assert key in src, f"Missing context-menu action {key}"


def test_context_menu_opens_on_row_right_click() -> None:
    src = _src()
    flat = re.sub(r"\s+", " ", src)
    # onContextMenu preventDefaults the native menu, stops the row's
    # open/toggle click, and opens the positioned menu at the cursor.
    assert "onContextMenu={function(e) {" in src
    assert "setCtxMenu({ item: item, x: e.clientX, y: e.clientY });" in flat
    assert "e.preventDefault(); e.stopPropagation(); setCtxMenu(" in flat


def test_context_menu_closes_on_outside_click_and_escape() -> None:
    src = _src()
    assert 'document.addEventListener("mousedown"' in src
    assert 'e.key === "Escape"' in src
    # The menu action dispatches then closes.
    assert "onAction(a.key, item);" in src
    assert "onClose();" in src


def test_context_menu_dispatch_wires_existing_handlers() -> None:
    src = _src()
    assert "function handleCtxAction(action, item) {" in src
    # Each action routes to the right handler (Open=handleFileClick,
    # Download=ST_triggerDownload, Delete=handleDelete, create/upload targeted).
    assert 'if (action === "open") handleFileClick(item);' in src
    assert 'else if (action === "download") ST_triggerDownload(wid, item.path);' in src
    assert 'else if (action === "rename") handleRename(item);' in src
    assert 'else if (action === "delete") handleDelete(item);' in src
    assert 'else if (action === "new-file") handleNewFileIn(item.path);' in src
    assert 'else if (action === "new-folder") handleNewFolderIn(item.path);' in src
    assert 'else if (action === "upload") handleUploadIn(item.path);' in src


def test_download_action_reuses_download_url() -> None:
    src = _src()
    # ST_triggerDownload builds the SAME /files/download URL the center editor's
    # download button uses, and clicks a synthesized anchor.
    assert "function ST_triggerDownload(wid, path) {" in src
    assert '"/files/download?path=" +' in src
    assert 'a.click();' in src


# ---------------------------------------------------------------------------
# Backend move endpoint (query params) — used by rename + drag-move
# ---------------------------------------------------------------------------


def test_rename_calls_move_endpoint_and_remaps_tab() -> None:
    src = _src()
    assert "async function handleRename(item) {" in src
    # promptDialog seeded with the current basename as the default value.
    assert "defaultValue: base," in src
    # POST /files/move?src=<old>&dst=<newpath>
    assert '/files/move?src=" +' in src
    assert '"&dst=" + encodeURIComponent(dst)' in src
    # Open editor tab is repointed to the new path.
    assert "ST_remapTabsForMove(oldPath, dst, isDir);" in src
    assert "studio.renameTab(" in src
    assert "handleRefresh();" in src


def test_rename_tab_remap_covers_folder_descendants() -> None:
    src = _src()
    # A folder rename repoints every open tab under it (prefix "file:<old>/").
    assert 'var childPrefix = "file:" + oldPath + "/";' in src
    assert 'tid.indexOf(childPrefix) === 0' in src


def test_studio_state_exposes_rename_tab() -> None:
    txt = STUDIO.read_text(encoding="utf-8")
    assert "var renameTab = React.useCallback(" in txt
    assert "renameTab: renameTab," in txt


# ---------------------------------------------------------------------------
# Drag-drop upload (OS files) vs drag-to-move (internal row)
# ---------------------------------------------------------------------------


def test_rows_are_draggable_and_stamp_source_path() -> None:
    src = _src()
    assert "draggable={true}" in src
    assert "onDragStart={function(e) { ST_dragStartRow(item, e); }}" in src
    # dragstart records the source path + kind on a custom DataTransfer type so
    # a drop can tell an internal move from an OS-file upload.
    assert '"application/x-primer-file"' in src
    assert 'effectAllowed = "move"' in src


def test_file_drag_vs_move_is_distinguished_by_files() -> None:
    src = _src()
    # OS-file drags carry dataTransfer.files / a "Files" type; internal moves
    # carry the custom x-primer-file payload instead.
    assert "function ST_isFileDrag(e) {" in src
    assert '"Files"' in src
    # The folder drop branches on files first (upload), else parses the move
    # payload.
    assert "if (dt && dt.files && dt.files.length) {" in src
    assert "await ST_uploadInto(item.path, dt.files);" in src
    assert 'dt.getData("application/x-primer-file")' in src
    assert "await doMove(src.path, !!src.is_dir, item.path);" in src


def test_folder_rows_are_drop_targets_with_highlight() -> None:
    src = _src()
    assert "onDragOver={function(e) { ST_dragOverFolder(item, e); }}" in src
    assert "onDrop={function(e) { ST_dropOnFolder(item, e); }}" in src
    # Highlight only a folder row that is the active drop target.
    assert "var isDropTarget = item.is_dir && dropTarget === item.path;" in src


def test_tree_root_accepts_os_file_upload() -> None:
    src = _src()
    assert 'data-testid="files-tree-body"' in src
    assert "onDragOver={ST_dragOverRoot}" in src
    assert "onDrop={ST_dropOnRoot}" in src
    # Root drop uploads into "" (workspace root); guarded to OS-file drags only.
    assert 'await ST_uploadInto("", dt.files);' in src
    assert "if (!ST_isFileDrag(e)) return;" in src


def test_upload_put_is_shared_and_base64() -> None:
    src = _src()
    # The base64 PUT lives in one shared helper reused by the picker + drag-drop.
    assert "async function ST_putUpload(dir, file) {" in src
    assert '{ content: b64, encoding: "base64" }' in src
    # Drag-drop and the picker both funnel through ST_uploadInto.
    assert "async function ST_uploadInto(dir, fileList) {" in src


def test_move_no_op_guards() -> None:
    src = _src()
    # doMove refuses self/descendant drops and same-dir drops (no-op).
    assert 'if (folderPath === srcPath || folderPath.indexOf(srcPath + "/") === 0) return;' in src
    assert "if (ST_pathDir(srcPath) === folderPath) return;" in src


# ---------------------------------------------------------------------------
# The existing header quick-buttons + their tests must be untouched.
# ---------------------------------------------------------------------------


def test_header_quick_buttons_retained() -> None:
    src = _src()
    for tid in (
        'data-testid="files-new-file"',
        'data-testid="files-upload"',
        'data-testid="files-new-folder"',
        'data-testid="files-upload-input"',
    ):
        assert tid in src


# ---------------------------------------------------------------------------
# Hard gate: the bundle still transpiles with all the new wiring.
# ---------------------------------------------------------------------------


def test_bundle_transpiles_with_dragdrop() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio-sidebar.jsx === */" in text
    for name in (
        "ST_FileContextMenu",
        "handleRename",
        "handleCtxAction",
        "ST_dropOnFolder",
        "doMove",
        "ST_putUpload",
    ):
        assert name in text, f"{name} missing from transpiled bundle"
