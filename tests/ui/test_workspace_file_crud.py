"""Static JSX checks for workspace file/folder create + delete affordances.

Reported via the bug button: bug-2026-06-06T082102Z-634f546a
"We should have the ability to create/update/delete any file or folder in
the workspace from the UI".
"""

from __future__ import annotations

from pathlib import Path

WORKSPACES = Path(__file__).resolve().parents[2] / "ui" / "components" / "workspaces.jsx"


def _src() -> str:
    return WORKSPACES.read_text(encoding="utf-8")


def test_new_file_and_folder_buttons_present() -> None:
    src = _src()
    assert 'data-testid="ws-new-file"' in src
    assert 'data-testid="ws-new-folder"' in src


def test_make_dir_posts_to_files_dir_endpoint() -> None:
    src = _src()
    assert "/files/dir?path=" in src
    assert '"POST"' in src


def test_recursive_delete_query_for_directories() -> None:
    src = _src()
    # The delete mutation must append recursive=true when removing a dir.
    assert 'recursive ? "&recursive=true" : ""' in src


def test_create_modal_path_input_present() -> None:
    src = _src()
    assert 'data-testid="ws-create-path"' in src


def test_tree_rows_expose_delete_affordance() -> None:
    src = _src()
    # Per-row delete button threaded through the tree.
    assert "ws-row-delete:" in src
    assert "onDelete" in src
