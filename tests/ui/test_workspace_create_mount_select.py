"""Task 10 — "Mount collections" multi-select in the New-workspace modal.

`WS_NewWorkspaceModal` (ui/components/workspaces.jsx) creates workspaces via
POST /v1/workspaces. The backend (Task 6) now accepts `mounts:
[{collection_id}]` at creation time. This adds a Set-based multi-select
(checkbox rows, mirroring the toggle idiom already used in agents.jsx) over
`GET /collections?limit=200` so an operator can choose which collections a
new workspace mounts, without requiring a native `<select multiple>`.

Static-source checks only (the tests/ui suite convention), plus the usual
build_jsx_bundle(UI) transpile gate.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
WORKSPACES = UI / "components" / "workspaces.jsx"


def _fn_block(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _modal_src() -> str:
    src = WORKSPACES.read_text(encoding="utf-8")
    return _fn_block(src, "function WS_NewWorkspaceModal(", "function WorkspaceDetail(")


def test_collections_resource_is_loaded() -> None:
    modal = _modal_src()
    assert "/collections?limit=200" in modal


def test_mount_selection_is_set_based_state() -> None:
    modal = _modal_src()
    assert "mountSel" in modal
    assert "new Set(" in modal


def test_on_create_builds_mounts_from_selected_ids() -> None:
    modal = _modal_src()
    assert "body.mounts" in modal
    assert "collection_id" in modal


def test_checkbox_row_has_test_id() -> None:
    modal = _modal_src()
    assert 'data-testid="create-mount-collection"' in modal


def test_mount_checkbox_labels_use_collection_id_not_description() -> None:
    # Regression: the multi-select rendered `{c.description || c.id}`, blowing
    # the modal open with full collection descriptions. Render the id instead.
    modal = _modal_src()
    assert "{c.description || c.id}" not in modal
    assert '<span className="mono">{c.id}</span>' in modal


def test_bundle_transpiles_with_workspace_create_mount_select() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/workspaces.jsx === */" in text
