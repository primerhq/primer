"""Regression: the slide-over drawer must render the Sidebar visibly.

The mobile media block hides `.sidebar` globally. MobileNav renders a
nested `<Sidebar />` inside `.drawer`, so without an explicit reveal
the drawer body collapses to empty. This test asserts that
`ui/styles.css` carries an override re-displaying the nested sidebar.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "ui" / "styles.css"


def _mobile_block() -> str:
    src = CSS.read_text(encoding="utf-8")
    m = re.search(r"@media\s*\(\s*max-width:\s*639px\s*\)\s*\{", src)
    assert m, "could not locate @media (max-width: 639px) block"
    start = m.end()
    depth = 1
    i = start
    while i < len(src) and depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start : i - 1]


def test_drawer_sidebar_reveal_rule_present() -> None:
    block = _mobile_block()
    pattern = re.compile(
        r"\.drawer\s+\.sidebar\s*\{[^}]*display\s*:\s*(flex|block|grid|revert)",
        re.DOTALL,
    )
    assert pattern.search(block), (
        "expected a '.drawer .sidebar { display: flex|block|grid|revert; ... }' "
        "rule inside the mobile media block to override the global "
        "'.sidebar { display: none; }' hide"
    )


def test_drawer_collapsed_labels_revealed() -> None:
    """If the desktop sidebar was collapsed, the drawer should still
    show labels — the user can't toggle the sidebar from the drawer."""
    block = _mobile_block()
    assert ".drawer .sidebar.is-collapsed" in block, (
        "expected a .drawer .sidebar.is-collapsed override so collapsed "
        "labels are revealed inside the drawer"
    )


def test_drawer_unconditional_label_reveal() -> None:
    """An earlier @media (max-width: 900px) rule hides .nav-item .label,
    .nav-item .count and .nav-group across every sidebar. The drawer's
    nested sidebar must override those even when .is-collapsed is NOT
    present (the default expanded desktop state)."""
    block = _mobile_block()
    for selector in (
        ".drawer .sidebar .nav-group",
        ".drawer .sidebar .nav-item .label",
        ".drawer .sidebar .nav-item .count",
    ):
        assert selector in block, (
            f"expected {selector!r} override in the mobile block so the "
            "drawer renders labels regardless of desktop collapsed state"
        )
