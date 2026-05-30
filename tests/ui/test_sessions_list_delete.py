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
    assert "_bulkDelete" in src, (
        "expected a _bulkDelete handler bound to the Delete N button"
    )
    # The button must actually bind it via onClick.
    assert "onClick={_bulkDelete}" in src, (
        "the bulk Delete button must bind its onClick to _bulkDelete"
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
