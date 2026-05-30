"""workspaces.jsx: list page uses CardList + Fab on mobile; detail
page uses MobileTabs with tabs Files / Sessions / Logs / Config."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "workspaces.jsx"

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_use_viewport() -> None:
    assert "useViewport" in _src()

def test_card_list_for_workspaces_list() -> None:
    assert "CardList" in _src()

def test_fab_for_new_workspace() -> None:
    src = _src()
    assert "Fab" in src
    assert "New workspace" in src

def test_mobile_tabs_for_detail() -> None:
    assert "MobileTabs" in _src()

def test_detail_tab_ids() -> None:
    src = _src()
    for tab in ("files", "sessions", "logs", "config"):
        assert tab in src, f"missing detail tab '{tab}'"
