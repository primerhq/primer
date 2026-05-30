"""semantic-search.jsx stacks single-column on mobile and uses
CardList for the SSP provider list."""
from __future__ import annotations
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "semantic-search.jsx"
)

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_use_viewport() -> None:
    assert "useViewport" in _src()

def test_card_list_for_ssp_providers() -> None:
    assert "CardList" in _src()

def test_mobile_stack_class() -> None:
    src = _src()
    assert "ssp-mobile-stack" in src or "semantic-search-mobile" in src
