"""harness_form.jsx uses useViewport so the form fields stack
single-column on mobile."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "harness_form.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_use_viewport() -> None:
    assert "useViewport" in _src()


def test_mobile_class_applied() -> None:
    src = _src()
    assert "harness-form-mobile" in src or "form-mobile" in src
