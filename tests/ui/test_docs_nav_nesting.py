"""Static JSX checks for the two-level docs left-nav.

No jsdom in the python toolchain, so these are structural assertions on
the source: the nested-nav surface (group + leaf components), the
clickable-overview navigation, the expand/collapse toggle, and the
section-index / default-doc handling of grouped items. The render
behaviour itself is covered by the live console smoke after merge.
"""

from __future__ import annotations

from pathlib import Path


DOCS = Path(__file__).resolve().parents[2] / "ui" / "components" / "docs.jsx"


def _src() -> str:
    return DOCS.read_text(encoding="utf-8")


def test_group_and_leaf_components_defined() -> None:
    src = _src()
    assert "function WSP_DocsNavGroup(" in src
    assert "function WSP_DocsNavLeaf(" in src
    assert "function WSP_DocsFirstLeafSlug(" in src


def test_group_header_is_clickable_to_overview() -> None:
    """Clicking the group label navigates to the group's overview slug."""
    src = _src()
    assert "group.overview && group.overview.slug" in src
    assert "navigate(`/docs/${overviewSlug}`)" in src


def test_group_header_is_expandable() -> None:
    """A chevron toggles the child list open/closed, and the group
    auto-expands when it contains the active doc or search is active."""
    src = _src()
    assert "setOpen((v) => !v)" in src
    assert "containsActive" in src
    assert "forceOpen" in src
    # Children render only when open.
    assert "open && (group.children || []).map(" in src


def test_left_nav_branches_on_group_flag() -> None:
    """The nav maps each item to a group renderer or a leaf renderer."""
    src = _src()
    assert "doc.group ? (" in src
    assert "<WSP_DocsNavGroup" in src
    assert "<WSP_DocsNavLeaf" in src


def test_children_are_indented_below_their_header() -> None:
    """Group children render at depth 2 (deeper indent than top-level
    leaves)."""
    src = _src()
    assert "depth={2}" in src
    assert "depth={1}" in src
    assert "depth >= 2 ? 40 : 28" in src


def test_section_index_handles_group_cards() -> None:
    """The section-index card grid links a group card to its overview."""
    src = _src()
    assert "doc.overview && doc.overview.slug" in src


def test_default_doc_pick_handles_groups() -> None:
    """The /docs default-first-doc pick resolves a group's first leaf
    instead of indexing slug.split('/')[1] on a group object."""
    src = _src()
    assert "WSP_DocsFirstLeafSlug(first.docs[0])" in src
