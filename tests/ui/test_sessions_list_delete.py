"""Regression: sessions-list.jsx must wire per-row cancel + delete
affordances and a working bulk-delete button.

The page previously rendered a dead `<Btn>Delete N</Btn>` with no
click handler and no per-row affordance, so users could only cancel
a session via the detail page.
"""

from __future__ import annotations

from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui"
    / "components"
    / "sessions-list.jsx"
)


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_cancel_endpoint_wired() -> None:
    assert "/cancel" in _src(), (
        "expected the per-row cancel button to POST to "
        "/workspaces/{ws}/sessions/{sid}/cancel"
    )


def test_delete_endpoint_wired() -> None:
    src = _src()
    assert '"DELETE"' in src or "'DELETE'" in src, (
        "expected the per-row delete button to issue a DELETE request"
    )


def test_bulk_delete_handler_bound() -> None:
    src = _src()
    # The Delete N button now opens a confirmation modal instead of
    # acting inline. The actual mutation lives in _bulkDeleteConfirmed,
    # invoked from the modal's onConfirm.
    assert "_openBulkDeleteConfirm" in src, (
        "expected an _openBulkDeleteConfirm opener for the Delete N button"
    )
    assert "_bulkDeleteConfirmed" in src, (
        "expected a _bulkDeleteConfirmed handler invoked from the confirm modal"
    )
    assert "onClick={_openBulkDeleteConfirm}" in src, (
        "the bulk Delete button must bind its onClick to the confirm opener"
    )


def test_row_actions_component_exists() -> None:
    src = _src()
    assert "RowActions" in src, (
        "expected a RowActions component rendering cancel/delete icons "
        "per row"
    )


def test_row_actions_appear_in_both_layouts() -> None:
    """Mobile cards and the desktop table both need the actions."""
    src = _src()
    assert 'layout="card"' in src, "RowActions must be wired into Card"
    assert 'layout="table"' in src, "RowActions must be wired into the table row"


def test_delete_actions_open_confirmation_modal() -> None:
    """Single-row delete, force-delete, and bulk-delete must all route
    through the confirmation modal — no path may call _deleteOne
    directly from a button onClick without user confirmation.

    The window.confirm escape hatch on force-delete must also be gone."""
    src = _src()
    assert "SL_DeleteConfirmModal" in src, (
        "expected a SL_DeleteConfirmModal component"
    )
    assert "setConfirm(" in src, (
        "expected setConfirm() calls to open the modal"
    )
    # No direct unconfirmed delete from a click handler — must go via
    # setConfirm({...}). The window.confirm fallback was a UX bandage
    # that has to be removed.
    assert "window.confirm" not in src, (
        "drop window.confirm — use the Modal-based SL_DeleteConfirmModal"
    )


def test_bulk_button_opens_confirm() -> None:
    src = _src()
    assert "_openBulkDeleteConfirm" in src
    assert "onClick={_openBulkDeleteConfirm}" in src, (
        "the Delete N button must open the confirm modal, not delete inline"
    )
