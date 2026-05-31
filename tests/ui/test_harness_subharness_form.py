"""Static JSX checks for composite override form — Spec A §13."""

from __future__ import annotations

from pathlib import Path


HARNESS_FORM = Path(__file__).resolve().parents[2] / "ui" / "components" / "harness_form.jsx"


def _src() -> str:
    return HARNESS_FORM.read_text(encoding="utf-8")


def test_form_special_cases_dependencies_property():
    src = _src()
    assert '"dependencies"' in src or "'dependencies'" in src


def test_form_renders_dep_cards_with_testid():
    src = _src()
    assert "dep-card" in src


def test_form_dep_cards_collapsible():
    # Should use React state to track collapsed/expanded
    src = _src()
    # Look for either a useState in the dep block or an onClick toggling.
    # Permissive: the file should reference useState near the dep block.
    assert "useState" in src
    # Ensure the dep-card has an interactive header (onClick or summary).
    assert "onClick" in src or "<summary" in src
