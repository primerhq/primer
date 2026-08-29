"""workspaces.jsx: list page uses a card grid + Fab on mobile; detail
page uses MobileTabs with tabs Files / Sessions / Logs / Config."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "workspaces.jsx"
CSS = Path(__file__).resolve().parents[2] / "ui" / "styles.css"

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def _styles() -> str:
    return CSS.read_text(encoding="utf-8")

def test_use_viewport() -> None:
    assert "useViewport" in _src()

def test_card_list_for_workspaces_list() -> None:
    """RETARGET (platform wave P1b item 7): the isMobile ? CardList :
    table split is gone - the reference anatomy is one .pc-card-grid
    (P1a's own class) for every viewport, so there is no JS branch left
    to pin. Assert the grid mechanism instead, mirroring the retarget
    already done for provider-catalog.jsx in P1a
    (test_provider_catalog_list.py::test_the_list_adapts_to_narrow_viewports).
    """
    src = _src()
    assert "pc-card-grid" in src
    css = _styles()
    assert ".pc-card-grid" in css

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
