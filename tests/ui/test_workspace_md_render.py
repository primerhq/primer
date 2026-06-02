"""Static JSX checks for the workspace file viewer's markdown render toggle.

Reported via the bug button: bug-2026-06-02T185919Z-b3919f59
"In the workspace, we're viewing a markdown file, ideally we should be
able to render the markdown instead of displaying the raw content."
"""

from __future__ import annotations

from pathlib import Path

WORKSPACES = Path(__file__).resolve().parents[2] / "ui" / "components" / "workspaces.jsx"


def _src() -> str:
    return WORKSPACES.read_text(encoding="utf-8")


def test_md_toggle_button_present() -> None:
    src = _src()
    assert 'data-testid="ws-file-md-toggle"' in src, (
        "workspace file viewer must surface a Raw/Rendered toggle for .md files"
    )


def test_md_toggle_keyed_to_md_extension() -> None:
    src = _src()
    # Both the toggle button and the rendered-content branch must gate on
    # the file having a .md extension (case-insensitive).
    assert '.toLowerCase().endsWith(".md")' in src


def test_rendered_branch_uses_window_renderMarkdown() -> None:
    src = _src()
    assert "window.renderMarkdown" in src, (
        "rendered branch must delegate to the existing window.renderMarkdown helper"
    )


def test_view_mode_state_defined() -> None:
    src = _src()
    assert "viewMode" in src
    assert "setViewMode" in src
