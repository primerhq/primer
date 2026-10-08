"""The Platform card grid refreshes when an overlay opened from it closes (ADM-16 of the 2026-10-08 admin review).

A card opens its entity in an overlay over the grid. A write made there (an edit, a rename, a delete inside the detail) never reached the grid behind it: the page's list is
a ``useResource`` polled every 15 s and only the page's own ``del()`` refetched it, so the old values stayed on screen for up to 15 s after the overlay closed (the finding's
repro: close the graph builder, and the grid still showed one graph for 2 s with no ``GET /v1/graphs`` issued).

The page now notes whether an overlay is open and refetches its list on the open -> closed transition. The decision is a pure function, ``NV_overlayClosed(wasOpen, isOpen)``,
which runs here in MiniRacer on the real source; where the page keeps the previous state and calls it is JSX, so that is a source check (this checkout has no render harness
for the page), and ``tests/ui_e2e/test_platform_overlay_close_refetch_journey.py`` drives the real page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLAT = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _closed(was_open, is_open) -> bool:
    from py_mini_racer import MiniRacer

    start = PLAT.index("function NV_overlayClosed(")
    end = PLAT.index("\n}\n", start) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(PLAT[start:end])
    return ctx.eval(f"NV_overlayClosed({str(was_open).lower()}, {str(is_open).lower()})")


@pytest.mark.parametrize(
    "was_open,is_open,expected",
    [(True, False, True), (True, True, False), (False, False, False), (False, True, False)],
)
def test_only_an_open_to_closed_transition_refetches(was_open: bool, is_open: bool, expected: bool) -> None:
    """Opening an overlay, and an overlay that stays open while its id changes, must not refetch; mounting with no overlay must not either."""
    assert _closed(was_open, is_open) is expected


def test_the_page_refetches_its_list_through_the_tested_decision() -> None:
    page = PLAT[PLAT.index("function NV_PlatPage("):PLAT.index("function NV_Platform(")]

    assert re.search(r"var overlayOpen = !!con\.overlay;", page), "the page must read whether the console has an overlay open"
    assert re.search(r"var overlayWasOpen = React\.useRef\(overlayOpen\);", page), "the previous state is kept in a ref, so the first render does not refetch"
    effect = re.search(
        r"React\.useEffect\(function \(\) \{\s*if \(NV_overlayClosed\(overlayWasOpen\.current, overlayOpen\)\) res\.refetch\(\);\s*overlayWasOpen\.current = overlayOpen;\s*\}, \[overlayOpen\]\);",
        page,
    )
    assert effect, "the effect must refetch on the open -> closed transition and then remember the new state"
    assert page.index("var res = window.primerApi.useResource(") < effect.start(), "the effect uses the list resource, which is declared first"
