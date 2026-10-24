"""workspaces.jsx: the detail page (a workspace's Settings) uses MobileTabs with tabs Files / Sessions / Logs / Config.

The list page and its phone FAB (a card grid and a "New workspace" Fab) are gone (platform revamp 08, Task 14; the user's decision C1): the phone lists the
workspaces under Spaces, and the console's one new-workspace dialog is a kit form overlay, a bottom sheet on a phone."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "workspaces.jsx"

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_use_viewport() -> None:
    assert "useViewport" in _src()

def test_the_list_page_and_its_fab_are_gone() -> None:
    src = _src()
    assert "function WorkspacesPage(" not in src and "pc-card-grid" not in src
    assert "<Fab" not in src and "New workspace" not in src

def test_mobile_tabs_for_detail() -> None:
    assert "MobileTabs" in _src()

def test_detail_tab_ids() -> None:
    src = _src()
    for tab in ("files", "sessions", "logs", "config"):
        assert tab in src, f"missing detail tab '{tab}'"
