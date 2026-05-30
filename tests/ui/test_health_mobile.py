"""health.jsx stacks metric grid single-column on mobile."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "health.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_use_viewport() -> None:
    assert "useViewport" in _src()


def test_mobile_stack_class() -> None:
    src = _src()
    assert "health-mobile-stack" in src or "metric-grid-mobile" in src
