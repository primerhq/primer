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


def _instance_list_src() -> str:
    """Just PC_InstanceList.

    The file also holds panels whose small option dropdowns fetch a flat
    list; the rule under test is about the INSTANCE list, so it is read
    from the function that owns it rather than from the whole module.
    """
    src = _src()
    start = src.index("function PC_InstanceList(")
    return src[start:src.index("\nfunction ", start + 1)]


def test_the_instance_list_is_server_paginated() -> None:
    src = _instance_list_src()
    assert "usePagedList" in src
    assert "<Pager" in src
    assert "limit=200" not in src, (
        "a hard limit=200 is the pattern usePagedList exists to replace"
    )


def test_the_list_adapts_to_narrow_viewports() -> None:
    src = _src()
    assert "useViewport" in src
    assert "isMobile" in src
    assert "CardList" in src


def test_the_empty_state_survived_the_rewrite() -> None:
    src = _src()
    assert "No " in src
    assert "provider-empty-" in src
