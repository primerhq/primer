"""The catalog's instance list is a kit paged list (Plan 01).

Pins PK_usePagedFilter with pageSize: 12 and the pc-empty: state.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _src() -> str:
    return (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")


def _styles() -> str:
    return (UI / "styles.css").read_text(encoding="utf-8")


def test_the_instance_list_is_kit_paged() -> None:
    src = _src()
    assert "PK_usePagedFilter" in src
    assert "pageSize: 12" in src


def test_the_list_adapts_to_narrow_viewports() -> None:
    css = _styles()
    assert ".pc-card-grid" in css
    grid_rule = css[css.index(".pc-card-grid"):]
    grid_rule = grid_rule[:grid_rule.index("}") + 1]
    assert "grid-template-columns" in grid_rule
    assert "auto-fill" in grid_rule or "auto-fit" in grid_rule, (
        "the grid must reflow its own column count, not rely on a fixed "
        "desktop/mobile JS branch"
    )


def test_the_empty_state_survived_the_rewrite() -> None:
    src = _src()
    assert "No " in src
    assert "pc-empty:" in src
