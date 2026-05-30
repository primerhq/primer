"""dashboard.jsx reads useViewport and applies a mobile-specific
single-column class on the metric grid."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "dashboard.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_uses_use_viewport() -> None:
    assert "useViewport" in _src()


def test_is_mobile_branch() -> None:
    assert "isMobile" in _src()


def test_mobile_stack_class_applied() -> None:
    src = _src()
    assert "metric-grid-mobile" in src or "dashboard-mobile" in src
