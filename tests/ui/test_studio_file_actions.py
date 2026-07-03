"""Structural checks for the Studio files-tree actions (New file / Upload / New folder).

The three actions are UI-only wiring over already-existing workspace file
endpoints (primer/api/routers/workspaces.py):

  * New file   → PUT  /v1/workspaces/{wid}/files?path=<rel>  { content:"", encoding:"text" }
  * Upload     → PUT  /v1/workspaces/{wid}/files?path=<rel>  { content:<base64>, encoding:"base64" }
  * New folder → POST /v1/workspaces/{wid}/files/dir?path=<rel>

These assertions guard that the FilesTree section header exposes the actions,
that each is wired to the right endpoint + payload shape, that the hidden
multi-file input + FileReader base64 handling exist, and that a freshly
created file opens in the center editor.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SIDEBAR = UI / "components" / "studio-sidebar.jsx"


def _src() -> str:
    return SIDEBAR.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# testids: the three action buttons + the hidden upload input are present
# ---------------------------------------------------------------------------


def test_file_action_testids_present() -> None:
    src = _src()
    for tid in (
        'data-testid="files-new-file"',
        'data-testid="files-upload"',
        'data-testid="files-new-folder"',
        'data-testid="files-upload-input"',
    ):
        assert tid in src, f"Missing data-testid: {tid}"


def test_action_buttons_are_keyboard_accessible() -> None:
    src = _src()
    # Each action button carries an aria-label (native <button> handles Enter/Space).
    for label in (
        'aria-label="New file"',
        'aria-label="Upload files"',
        'aria-label="New folder"',
    ):
        assert label in src, f"Missing aria-label: {label}"


# ---------------------------------------------------------------------------
# New file → PUT /files?path= with text encoding, no etag (create)
# ---------------------------------------------------------------------------


def test_new_file_puts_text_content_without_etag() -> None:
    src = _src()
    assert 'apiFetch(\n        "PUT",' in src or '"PUT"' in src
    # Writes the create payload (empty text file) — no etag query param.
    assert '{ content: "", encoding: "text" }' in src
    assert '/files?path=" + encodeURIComponent(name)' in src
    # A create must NOT append an etag (etag is overwrite-only concurrency).
    assert "etag=" not in src, "New file create must omit the etag query param"


def test_new_file_opens_in_editor() -> None:
    src = _src()
    # After create, the new file opens as a center tab in edit mode.
    assert "ST_openNewFileTab" in src
    assert 'kind: "file"' in src
    assert 'mode: "edit"' in src
    # Opens via the same studio tab-open path the file rows use.
    assert "studio.openTab({" in src


# ---------------------------------------------------------------------------
# Upload → hidden file input + FileReader base64 → PUT with base64 encoding
# ---------------------------------------------------------------------------


def test_upload_uses_hidden_multi_file_input() -> None:
    src = _src()
    assert 'type="file"' in src
    assert "multiple" in src
    assert 'style={{ display: "none" }}' in src
    assert "uploadInputRef" in src
    # Reset after upload so re-picking the same file re-fires change.
    assert 'input.value = ""' in src


def test_upload_reads_base64_and_puts_base64_encoding() -> None:
    src = _src()
    assert "FileReader" in src
    assert "readAsDataURL" in src
    # Strips the "data:...;base64," prefix (everything up to and incl. the comma).
    assert 'indexOf(",")' in src
    assert "slice(comma + 1)" in src
    # PUT with the raw base64 payload.
    assert "{ content: b64, encoding: \"base64\" }" in src


# ---------------------------------------------------------------------------
# New folder → POST /files/dir?path=
# ---------------------------------------------------------------------------


def test_new_folder_posts_to_files_dir_endpoint() -> None:
    src = _src()
    assert '"POST"' in src
    assert '/files/dir?path=" + encodeURIComponent(name)' in src


# ---------------------------------------------------------------------------
# Mutations refetch the tree (so new entries appear)
# ---------------------------------------------------------------------------


def test_actions_refetch_tree() -> None:
    src = _src()
    # Each success path calls handleRefresh() (clears the folder cache + refetches root).
    assert src.count("handleRefresh();") >= 3


# ---------------------------------------------------------------------------
# The bundle (incl. the new wiring) still transpiles cleanly — the hard gate.
# ---------------------------------------------------------------------------


def test_bundle_transpiles_with_file_actions() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio-sidebar.jsx === */" in text
    # The new handlers survive transpilation.
    assert "handleNewFile" in text
    assert "handleUploadChange" in text
    assert "handleNewFolder" in text
