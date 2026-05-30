"""knowledge.jsx mobile: collections list takes the whole screen;
tapping a collection navigates to a full-screen documents list. The
two-column desktop grid does not render on mobile."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "knowledge.jsx"

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_use_viewport() -> None:
    assert "useViewport" in _src()

def test_card_list_for_collections() -> None:
    assert "CardList" in _src()

def test_breadcrumb_back_on_mobile_documents() -> None:
    src = _src()
    assert "knowledge-mobile-back" in src or "breadcrumb-back" in src

def test_fab_for_new_collection() -> None:
    src = _src()
    assert "Fab" in src
    assert "New collection" in src or "New document" in src
