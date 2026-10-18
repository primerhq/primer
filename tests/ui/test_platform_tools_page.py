"""The Tools page loads on open and on Refresh instead of polling, and its filter finds a toolset's tools by the toolset's id (Platform revamp Plan 01, Task 11; design 13.5).

``GET /v1/tools/catalogue`` lists every toolset live with a timeout of ten seconds each, and the page re-polled it every fifteen seconds, which is a slow request made four times a minute for as
long as the page stayed open. The page entry now says ``poll: false`` (no ``pollMs``) and ``refresh: true`` (the header gets a Refresh button that reloads it), and the other pages keep polling at
``NV_PLAT_POLL_MS``. A tool is found by the toolset it belongs to: the card is named by the bare tool name and its id is the scoped ``<toolset>__<tool>``, which the filter matches (Task 10's
``NV_platFilterText``).

The page runs here for real (``tests/ui/_platform_page_harness.py``). ``useResource`` is the harness's stand-in, wrapped here to keep each fetcher it is handed, so the page's own counting of the
loads in flight can be driven: the real hook reports ``loading`` only for the first load of a resource (a refetch keeps the data it has and says nothing), so without that count a Refresh would
never read as busy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._platform_page_harness import platform_navs, platform_page_context

ROOT = Path(__file__).resolve().parents[2]
MOBILE = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")

KEEP_FETCHERS = """
var FETCHERS = {};
(function () {
  var inner = window.primerApi.useResource;
  window.primerApi.useResource = function (key, fn, opts) { FETCHERS[key] = fn; return inner(key, fn, opts); };
})();
"""


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def page_ctx():
    ctx = platform_page_context(prelude_extra=KEEP_FETCHERS)
    try:
        yield ctx
    finally:
        ctx.close()


def _tools(ctx, *ids: str) -> None:
    ctx.eval(f"ITEMS = {json.dumps([{'id': i} for i in ids])}; CON.view = {{ name: 'platform', nav: 'tools', sub: null }}; MR.mount(NV_PlatListPage, {{ nav: 'tools' }});")


def _ids(ctx) -> list[str]:
    return js(ctx, "MR.findAll('pk-card:').map(function (e) { return e.props['data-testid'].slice('pk-card:'.length); })")


def _label(ctx) -> str:
    return ctx.eval("MR.find('nv-plat-refresh').props.children")


# ---- the page entry and the polling ------------------------------------------------------------------------------------------------------------------


def test_the_tools_page_does_not_poll(page_ctx) -> None:
    _tools(page_ctx, "web__search")
    assert js(page_ctx, 'RESOURCE_OPTS["nv-plat:tools"]') == {"pollMs": 0, "deps": ["tools"]}
    page_ctx.eval("CON.view = { name: 'platform', nav: 'agents', sub: null }; ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    assert js(page_ctx, 'RESOURCE_OPTS["nv-plat:agents"]') == {"pollMs": 15000, "deps": ["agents"]}, "the other pages keep their 15 s poll"


def test_only_the_tools_entry_stops_polling_and_asks_for_a_refresh(page_ctx) -> None:
    assert page_ctx.eval("NV_PLAT_PAGES.tools.poll === false && NV_PLAT_PAGES.tools.refresh === true")
    others = [nav for nav in platform_navs() if nav != "tools"]
    for nav in others:
        assert page_ctx.eval(f"NV_PLAT_PAGES[{json.dumps(nav)}].poll === undefined && NV_PLAT_PAGES[{json.dumps(nav)}].refresh === undefined"), nav
    assert page_ctx.eval("NV_PLAT_POLL_MS") == 15000


def test_the_phone_list_reads_the_same_poll_flag() -> None:
    """The phone's section list shares the page's cache key (``nv-plat:<nav>``) and its entries, so it would otherwise keep polling the slow catalogue every 15 s on a phone."""
    block = MOBILE[MOBILE.index("function NV_MobilePlatform("):MOBILE.index("function NV_MobileSystemScreen(")]
    assert re.search(r"pollMs: page && page\.poll === false \? 0 : 15000, deps: \[nav\]", block)


# ---- Refresh ---------------------------------------------------------------------------------------------------------------------------------


def test_refresh_is_a_secondary_button_in_the_header_and_refetches(page_ctx) -> None:
    _tools(page_ctx, "web__search")
    ids = [e.get("testid") for e in js(page_ctx, 'MR.subtree("pk-header")') if e.get("testid")]
    assert ids.index("nv-plat-refresh") < ids.index("nv-plat-filter"), "a secondary control sits left of the filter"
    assert page_ctx.eval("MR.find('nv-plat-refresh').props.className") == "pk-extra"
    assert page_ctx.eval("MR.find('nv-plat-refresh').props.type") == "button"
    assert _label(page_ctx) == "Refresh"
    assert page_ctx.eval("MR.find('nv-plat-refresh').props['aria-busy'] === undefined && MR.find('nv-plat-refresh').props['aria-disabled'] === undefined")
    page_ctx.eval("MR.click('nv-plat-refresh')")
    assert page_ctx.eval("REFETCHES") == 1


def test_refresh_says_so_while_the_first_load_is_still_on_its_way_and_a_click_then_does_nothing(page_ctx) -> None:
    page_ctx.eval("ITEMS = null; LOADING = true; CON.view = { name: 'platform', nav: 'tools', sub: null }; MR.mount(NV_PlatListPage, { nav: 'tools' });")
    assert _label(page_ctx) == "Refreshing…"
    assert page_ctx.eval("MR.find('nv-plat-refresh').props['aria-busy']") == "true"
    assert page_ctx.eval("MR.find('nv-plat-refresh').props['aria-disabled']") == "true"
    page_ctx.eval("MR.click('nv-plat-refresh')")
    assert page_ctx.eval("REFETCHES") == 0


def test_refresh_says_so_with_data_on_screen_while_the_loading_flag_is_up(page_ctx) -> None:
    """The case the harness can set directly: data present and ``loading`` true."""
    _tools(page_ctx, "web__search")
    page_ctx.eval("LOADING = true; MR.rerender();")
    assert _label(page_ctx) == "Refreshing…" and page_ctx.eval("MR.find('nv-plat-refresh').props['aria-disabled']") == "true"
    page_ctx.eval("MR.click('nv-plat-refresh')")
    assert page_ctx.eval("REFETCHES") == 0
    page_ctx.eval("LOADING = false; MR.rerender();")
    assert _label(page_ctx) == "Refresh"
    page_ctx.eval("MR.click('nv-plat-refresh')")
    assert page_ctx.eval("REFETCHES") == 1


def test_a_refetch_in_flight_reads_refreshing_until_it_settles(page_ctx) -> None:
    """The real hook keeps ``loading`` false for a refetch that has data to show, so the page counts the fetches it starts: Refreshing… from the moment one starts to the moment it ends."""
    _tools(page_ctx, "web__search")
    page_ctx.eval("var DONE; NV_PLAT_PAGES.tools.list = function () { return new Promise(function (resolve) { DONE = resolve; }); };"
                  "FETCHERS['nv-plat:tools'](null); MR.rerender();")
    assert _label(page_ctx) == "Refreshing…" and page_ctx.eval("MR.find('nv-plat-refresh').props['aria-busy']") == "true"
    page_ctx.eval("MR.click('nv-plat-refresh')")
    assert page_ctx.eval("REFETCHES") == 0, "a click while a load is in flight does nothing"
    page_ctx.eval("DONE({ items: [] }); ")
    page_ctx.eval("MR.rerender();")
    assert _label(page_ctx) == "Refresh" and page_ctx.eval("MR.find('nv-plat-refresh').props['aria-busy'] === undefined")
    page_ctx.eval("MR.click('nv-plat-refresh')")
    assert page_ctx.eval("REFETCHES") == 1


def test_a_refetch_that_fails_stops_saying_refreshing(page_ctx) -> None:
    _tools(page_ctx, "web__search")
    page_ctx.eval("var FAIL, SEEN = null; NV_PLAT_PAGES.tools.list = function () { return new Promise(function (resolve, reject) { FAIL = reject; }); };"
                  "FETCHERS['nv-plat:tools'](null).catch(function (e) { SEEN = e.message; }); MR.rerender();")
    assert _label(page_ctx) == "Refreshing…"
    page_ctx.eval("FAIL(new Error('down'));")
    page_ctx.eval("MR.rerender();")
    assert _label(page_ctx) == "Refresh"
    assert page_ctx.eval("SEEN") == "down", "the failure still reaches the hook, which shows it and retries"


def test_two_loads_in_flight_keep_it_busy_until_both_have_settled(page_ctx) -> None:
    """A second Refresh cannot start while one is in flight, but the hook's own triggers can (a tab that becomes visible, the idle wake-up): the count must not drop to idle at the first."""
    _tools(page_ctx, "web__search")
    page_ctx.eval("var DONES = []; NV_PLAT_PAGES.tools.list = function () { return new Promise(function (resolve) { DONES.push(resolve); }); };"
                  "FETCHERS['nv-plat:tools'](null); FETCHERS['nv-plat:tools'](null); MR.rerender();")
    page_ctx.eval("DONES[0]({ items: [] });")
    page_ctx.eval("MR.rerender();")
    assert _label(page_ctx) == "Refreshing…", "one load is still on its way"
    page_ctx.eval("DONES[1]({ items: [] });")
    page_ctx.eval("MR.rerender();")
    assert _label(page_ctx) == "Refresh"


def test_the_fetcher_hands_the_list_its_answer_untouched(page_ctx) -> None:
    _tools(page_ctx, "web__search")
    page_ctx.eval("var GOT = null; NV_PLAT_PAGES.tools.list = function (apiFetch, signal) { return Promise.resolve({ items: [{ id: 'x' }], signal: signal }); };"
                  "FETCHERS['nv-plat:tools']('SIG').then(function (r) { GOT = r; });")
    assert js(page_ctx, "GOT") == {"items": [{"id": "x"}], "signal": "SIG"}


def test_a_page_without_refresh_draws_no_refresh_button_and_wraps_no_fetcher(page_ctx) -> None:
    page_ctx.eval("ITEMS = [{ id: 'a1' }]; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    assert page_ctx.eval("MR.find('nv-plat-refresh') === null")
    page_ctx.eval("var CALLED = 0; NV_PLAT_PAGES.agents.list = function () { CALLED += 1; return Promise.resolve({ items: [] }); }; FETCHERS['nv-plat:agents'](null); MR.rerender();")
    assert page_ctx.eval("CALLED") == 1 and page_ctx.eval("MR.find('nv-plat-refresh') === null")


# ---- the filter -----------------------------------------------------------------------------------------------------------------------------


def test_the_filter_matches_a_toolset_id(page_ctx) -> None:
    _tools(page_ctx, "web__search", "web__fetch", "system__agents_find")
    page_ctx.eval('MR.find("nv-plat-filter").props.onChange({ target: { value: "web" } }); MR.rerender();')
    assert _ids(page_ctx) == ["web__search", "web__fetch"]
    page_ctx.eval('MR.find("nv-plat-filter").props.onChange({ target: { value: "SYSTEM__" } }); MR.rerender();')
    assert _ids(page_ctx) == ["system__agents_find"], "case does not matter, and a double underscore finds the toolset's own tools"


def test_the_tools_page_keeps_its_cards_ids_scoped_so_two_toolsets_may_each_have_a_search(page_ctx) -> None:
    _tools(page_ctx, "web__search", "docs__search")
    assert _ids(page_ctx) == ["web__search", "docs__search"]
    assert js(page_ctx, "MR.texts().filter(function (t) { return t === 'search'; }).length") == 2
