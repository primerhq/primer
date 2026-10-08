"""A list that is empty says so, and a filter that hides every row says that instead (finding M3 of the lead's sweep).

The Platform lists used one phrase for two different things. On desktop an EMPTY list said "Nothing here yet." even when a filter was hiding
rows (and offered "New agent" as if there were none); on mobile an empty list said "No matches." with NO filter set (the Triggers screenshot);
the new-session pickers said "No agent or graph matches." when there simply were no agents or graphs. The header also counted "1 entity" /
"3 entities", which is the code's word, not the user's.

The wording is two pure functions in ``nv-platform.jsx`` (``NV_emptyText``, ``NV_countText``), exported on ``window`` for the mobile shell and the
overlays, and every Platform page declares its own noun. They are driven here through MiniRacer against the real source. The call sites are
checked as source text: there is no render harness for these components in this checkout, so the journey in ``tests/ui_e2e`` is what proves
the desktop list; the mobile list and the two pickers are covered here only by the text check that they call the helper.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
PLAT = (CONSOLE / "nv-platform.jsx").read_text(encoding="utf-8")
MOBILE = (CONSOLE / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
OVERLAYS = (CONSOLE / "nv-overlays.jsx").read_text(encoding="utf-8")
PROVIDERS = (ROOT / "ui" / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")

# Every V8 isolate this file creates, closed after each test (tests/ui peaks at over a gigabyte for the isolates nobody disposes).
_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _helpers_src() -> str:
    start = PLAT.index("function NV_emptyText(")
    end = PLAT.index("// Per-entity page config.")
    return PLAT[start:end]


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(_helpers_src())
    return ctx


def _empty(noun: list[str], query: str, total: int) -> str:
    return _ctx().eval(f"NV_emptyText({json.dumps(noun)}, {json.dumps(query)}, {total})")


def _count(noun: list[str], shown: int, total: int) -> str:
    return _ctx().eval(f"NV_countText({json.dumps(noun)}, {shown}, {total})")


AGENT = ["agent", "agents"]


# ---- the empty text ---------------------------------------------------------------------------------------------------------------


def test_an_empty_list_with_no_filter_says_it_is_empty() -> None:
    assert _empty(AGENT, "", 0) == "No agents yet."


def test_a_filter_over_an_empty_list_still_says_it_is_empty() -> None:
    """There is nothing to match against: "No agents match" would blame the filter for a list that has no rows."""
    assert _empty(AGENT, "xyz", 0) == "No agents yet."


def test_a_filter_that_hides_every_row_says_nothing_matches_it() -> None:
    assert _empty(AGENT, "xyz", 7) == 'No agents match "xyz".'


def test_a_blank_filter_is_no_filter() -> None:
    assert _empty(AGENT, "   ", 7) == "No agents yet."


def test_the_filter_text_is_shown_trimmed() -> None:
    assert _empty(AGENT, "  zed ", 3) == 'No agents match "zed".'


# ---- the count --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shown,total,expected",
    [(0, 0, "0 agents"), (1, 1, "1 agent"), (12, 12, "12 agents"), (3, 12, "3 of 12 agents"), (1, 12, "1 of 12 agents"), (0, 5, "0 of 5 agents")],
)
def test_the_count_uses_the_pages_noun_and_says_how_many_a_filter_hides(shown: int, total: int, expected: str) -> None:
    assert _count(AGENT, shown, total) == expected


# ---- every page has a noun, and the call sites use the helpers ----------------------------------------------------------------


def _pages_ctx():
    """The helpers AND the page table, evaluated as written (no JSX in that stretch of the file)."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = globalThis;")
    ctx.eval(PLAT[PLAT.index("function NV_fact("):PLAT.index("var NV_PLAT_PAGE_SIZE")])
    return ctx


def test_every_platform_page_declares_its_noun() -> None:
    """Every KEY of the table, not every entry that has a create button: the Tools page has none, and the page renders the count for every key."""
    ctx = _pages_ctx()

    keys = json.loads(ctx.eval("JSON.stringify(Object.keys(NV_PLAT_PAGES))"))

    assert len(keys) >= 13 and "tools" in keys, keys
    for key in keys:
        noun = json.loads(ctx.eval(f"JSON.stringify(NV_PLAT_PAGES[{json.dumps(key)}].noun)"))
        assert isinstance(noun, list) and len(noun) == 2 and all(isinstance(w, str) and w for w in noun), (key, noun)


@pytest.mark.parametrize("noun", ["undefined", "null", "[]", '["only"]', '"agents"', "[1, 2]", '["", ""]'])
def test_a_missing_or_malformed_noun_degrades_to_item_items_instead_of_crashing(noun: str) -> None:
    """A page that forgets its noun must not blank the console (the page calls these helpers on every render, with no error boundary around it)."""
    ctx = _ctx()

    assert ctx.eval(f'NV_emptyText({noun}, "", 0)') == "No items yet."
    assert ctx.eval(f'NV_emptyText({noun}, "x", 3)') == 'No items match "x".'
    assert ctx.eval(f"NV_countText({noun}, 1, 1)") == "1 item"
    assert ctx.eval(f"NV_countText({noun}, 2, 5)") == "2 of 5 items"


def test_the_mobile_list_says_nothing_empty_while_loading_or_after_an_error() -> None:
    """"No triggers yet." while the list is still loading, or after the fetch failed, states something nobody has verified."""
    block = MOBILE[MOBILE.index("function NV_MobilePlatform("):MOBILE.index("function NV_MobileMore(")]

    assert re.search(r"!visible\.length && !res\.loading && !res\.error", block), "the empty text needs the same guard as the desktop page"
    assert re.search(r"res\.error \?[\s\S]{0,200}res\.error\.(detail|message)", block), "a failed fetch must say so instead of drawing a blank list"


def test_both_pickers_pass_the_unfiltered_row_count_as_the_total() -> None:
    """`visible.length` as the total would call a filter that hides everything "No agents or graphs yet."."""
    for name, source in (("mobile shell", MOBILE), ("overlays", OVERLAYS)):
        assert re.search(
            r'NV_emptyText\(\s*\["agent or graph", "agents or graphs"\], q, rows\.length\)', source
        ), f"{name}: the new-session picker must pass rows.length (the list before the filter)"


def test_the_clear_filter_button_has_a_test_id() -> None:
    assert re.search(r'data-testid="nv-plat-clear-filter"[\s\S]{0,300}Clear filter', PLAT)


def test_the_helpers_are_exported_for_the_mobile_shell_and_the_overlays() -> None:
    assert "window.NV_emptyText = NV_emptyText" in PLAT and "window.NV_countText = NV_countText" in PLAT


def test_the_desktop_header_and_empty_state_use_the_helpers() -> None:
    assert "NV_countText(page.noun" in PLAT and "NV_emptyText(page.noun" in PLAT
    assert "Nothing here yet." not in PLAT and '" entities"' not in PLAT


def test_the_mobile_list_and_pickers_use_the_helper_not_a_fixed_phrase() -> None:
    assert "No matches." not in MOBILE
    assert "window.NV_emptyText(page.noun" in MOBILE
    assert "No agent or graph matches." not in MOBILE and "No agent or graph matches." not in OVERLAYS


def test_the_providers_count_says_providers() -> None:
    assert '"entity"' not in PROVIDERS and '"entities"' not in PROVIDERS
    assert 'entityCount === 1 ? "provider" : "providers"' in PROVIDERS
