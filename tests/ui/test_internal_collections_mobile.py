"""internal-collections.jsx stacks Configure / Bootstrap / Active cards
single-column on mobile."""
from __future__ import annotations
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "internal-collections.jsx"
)

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_use_viewport() -> None:
    assert "useViewport" in _src()

def test_mobile_stack_class_applied() -> None:
    src = _src()
    assert "ic-mobile-stack" in src or "internal-collections-mobile" in src
