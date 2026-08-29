"""The catalog's instance list is a real list, not a limit=200 dump.

The pages it replaces paginate and adapt to mobile
(ui/components/providers.jsx:285,311,423). Losing that on the way into
the catalog would be a regression dressed up as a consolidation.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _src() -> str:
    return (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")


def _styles() -> str:
    return (UI / "styles.css").read_text(encoding="utf-8")


def _instance_list_src() -> str:
    """Just PC_InstanceGrid.

    The file also holds panels whose small option dropdowns fetch a flat
    list; the rule under test is about the INSTANCE list, so it is read
    from the function that owns it rather than from the whole module.

    RENAMED (platform wave P1a item 3): PC_InstanceList -> PC_InstanceGrid,
    a real card grid rather than a flat row list - see
    test_the_list_adapts_to_narrow_viewports below for how it now
    handles narrow viewports.
    """
    src = _src()
    start = src.index("function PC_InstanceGrid(")
    return src[start:src.index("\nfunction ", start + 1)]


def test_the_instance_list_is_server_paginated() -> None:
    src = _instance_list_src()
    assert "usePagedList" in src
    assert "<Pager" in src
    assert "limit=200" not in src, (
        "a hard limit=200 is the pattern usePagedList exists to replace"
    )


def test_the_list_adapts_to_narrow_viewports() -> None:
    """RETARGET (platform wave P1a item 3): the flat row list's isMobile/
    CardList branch (one layout for narrow, another for wide) is gone -
    the reference anatomy is a card GRID everywhere, and a CSS grid that
    reflows its own column count needs no JS viewport branch at all to
    adapt. Pin the CSS mechanism instead of the retired JS one.
    """
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
    assert "provider-empty-" in src
