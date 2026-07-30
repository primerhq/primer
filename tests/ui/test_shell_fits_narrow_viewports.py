"""The app shell's sizing contract for narrow viewports.

`.topbar` and `.main` are both grid items of `.app`. A grid item defaults
to `min-width: auto`, which floors it at its min-content width -- for the
topbar that is ~410px, so on any phone the shell forced the whole document
to scroll sideways. Every console page inherited it.

Fast static guards so a revert fails in CI rather than only in the E2E
workflow. The geometry itself is measured in
tests/ui_e2e/test_mobile_no_horizontal_overflow.py.
"""

from __future__ import annotations

from pathlib import Path

STYLES = Path(__file__).resolve().parents[2] / "ui" / "styles.css"


def _rule(selector: str) -> str:
    """The declaration block for `selector`, matched at the start of a line so
    `.topbar` does not pick up `.topbar-right`."""
    css = STYLES.read_text(encoding="utf-8")
    needle = f"\n{selector} {{"
    start = css.index(needle)
    return css[start:css.index("}", start)]


def test_the_topbar_may_shrink_below_its_min_content() -> None:
    assert "min-width: 0" in _rule(".topbar")


def test_the_scroll_region_may_shrink_below_its_min_content() -> None:
    # Without this, wide page content widens .main itself rather than
    # scrolling inside it, taking the document along.
    assert "min-width: 0" in _rule(".main")
    assert "overflow: auto" in _rule(".main")


def test_the_status_cluster_and_its_children_may_shrink() -> None:
    # Both levels are needed: the cluster is the widest item in the row, and
    # its own children (the worker pill) are what must actually give.
    assert "min-width: 0" in _rule(".topbar-right")
    assert "min-width: 0" in _rule(".topbar-right > *")


def test_the_worker_pill_clips_instead_of_wrapping() -> None:
    # It can shrink now, so it needs to say what happens when it does.
    block = _rule(".worker-pill")
    assert "white-space: nowrap" in block
    assert "text-overflow: ellipsis" in block
    assert "overflow: hidden" in block
