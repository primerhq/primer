"""A Platform page can say what its empty state means and offer the way out (Platform revamp Plan 01, Task 4; seam I-9).

A page entry may set ``emptyText`` and ``emptyAction: {label, run(con)}``. With no rows the empty state shows ``emptyText`` (else the generic
``NV_emptyText``) and a button (``nv-plat-empty-action``) that runs ``emptyAction.run(con)``; without ``emptyAction`` the page keeps its create
button when it has one. A filter that hides every row is unchanged ("No X match" and Clear filter). The LLM profiles page uses both hooks: it has
no single-profile create (design 7.1, the lead's settlement: single profiles come from the Register provider overlay's import), so its empty state
points at Providers › LLM, and its one header action is "+ Aggregate" (Plan 03, Task 2), which hosts the aggregated profile overlay. The phone list
reads the same two hooks (``tests/ui/test_mobile_more_reachability.py``).

The page runs here for real (``tests/ui/_platform_page_harness.py``).
"""

from __future__ import annotations

import json

import pytest

from tests.ui._platform_page_harness import platform_page_context

PROBE = ('NV_PLAT_PAGES.probe = { title: "Probes", noun: ["probe", "probes"], list: function () {},'
         ' card: function (r) { return { id: r.id, name: r.id, sub: "", facts: [] }; }, open: rec("open"),'
         ' emptyText: "Nothing to probe yet.",'
         ' emptyAction: { label: "Open Providers › LLM", run: function (con) { con.goView("platform", "providers", "llm"); } } };')


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def page_ctx():
    ctx = platform_page_context()
    try:
        yield ctx
    finally:
        ctx.close()


def _type_filter(ctx, text: str) -> None:
    ctx.eval(f'MR.find("nv-plat-filter").props.onChange({{ target: {{ value: {json.dumps(text)} }} }}); MR.rerender();')


def test_a_page_with_empty_text_and_action_shows_them_when_it_has_no_rows(page_ctx) -> None:
    page_ctx.eval(PROBE + " ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'probe' });")
    assert "Nothing to probe yet." in js(page_ctx, "MR.texts()")
    assert "No probes yet." not in js(page_ctx, "MR.texts()"), "the page's own words replace the generic sentence"
    assert page_ctx.eval('MR.find("nv-plat-empty-action").type') == "button"
    assert page_ctx.eval('MR.find("nv-plat-empty-action").props.type') == "button"
    assert page_ctx.eval('MR.find("nv-plat-empty-action").props.children') == "Open Providers › LLM"
    page_ctx.eval('MR.click("nv-plat-empty-action")')
    assert ["goView", "platform", "providers", "llm"] in js(page_ctx, "CALLS")


def test_the_empty_action_gets_the_console(page_ctx) -> None:
    page_ctx.eval(PROBE + " var GOT = null; NV_PLAT_PAGES.probe.emptyAction.run = function (con) { GOT = con; };"
                  " ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'probe' }); MR.click('nv-plat-empty-action');")
    assert page_ctx.eval("GOT === CON")


def test_a_filter_typed_on_an_empty_page_still_shows_the_pages_own_empty_state(page_ctx) -> None:
    page_ctx.eval(PROBE + " ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'probe' });")
    _type_filter(page_ctx, "zzz")
    assert "Nothing to probe yet." in js(page_ctx, "MR.texts()"), "there is nothing to match: the page is empty, whatever the filter"
    assert page_ctx.eval('!!MR.find("nv-plat-empty-action")') and page_ctx.eval('MR.find("nv-plat-clear-filter") === null')


def test_a_filter_that_hides_every_row_still_offers_clear_filter(page_ctx) -> None:
    page_ctx.eval(PROBE + " ITEMS = [{ id: 'a1' }, { id: 'a2' }]; MR.mount(NV_PlatListPage, { nav: 'probe' });")
    assert page_ctx.eval('MR.find("nv-plat-empty") === null'), "rows are drawn, no empty state"
    _type_filter(page_ctx, "zzz")
    texts = js(page_ctx, "MR.texts()")
    assert 'No probes match "zzz".' in texts and "Nothing to probe yet." not in texts
    assert page_ctx.eval('!!MR.find("nv-plat-clear-filter")')
    assert page_ctx.eval('MR.find("nv-plat-empty-action") === null'), "the way out of a filter is Clear filter, not the page's empty action"


def test_a_page_without_hooks_keeps_the_generic_empty_state(page_ctx) -> None:
    page_ctx.eval("ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    texts = js(page_ctx, "MR.texts()")
    assert "No agents yet." in texts
    assert page_ctx.eval('MR.find("nv-plat-empty-action") === null')
    buttons = js(page_ctx, 'MR.subtree("nv-plat-empty").filter(function (e) { return e.type === "button"; })')
    assert len(buttons) == 1, "today's create button stays"
    assert texts.count(page_ctx.eval("NV_PLAT_PAGES.agents.createLabel")) == 2, "the header's create and the empty state's"


def test_a_page_with_no_create_and_no_hooks_offers_no_button(page_ctx) -> None:
    page_ctx.eval("ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'tools' });")
    assert "No tools yet." in js(page_ctx, "MR.texts()")
    assert js(page_ctx, 'MR.subtree("nv-plat-empty").filter(function (e) { return e.type === "button"; })') == []


def test_the_profiles_entry_offers_aggregate_and_points_to_providers_llm_when_empty(page_ctx) -> None:
    p = "NV_PLAT_PAGES.profiles"
    assert page_ctx.eval(f"{p}.createLabel") == "+ Aggregate", "design 7.1: no New profile, one action"
    page_ctx.eval(f"MODALS = []; {p}.create(CON, function (m) {{ MODALS.push(m); }});")
    assert js(page_ctx, "MODALS") == [{"kind": "profile", "row": None}], "the action hosts the profile overlay with no row: the dispatcher draws the aggregate create form"
    assert page_ctx.eval(f"{p}.emptyText") == "No LLM profiles yet. Register an LLM provider to import its models."
    assert page_ctx.eval(f"{p}.emptyAction.label") == "Open Providers › LLM"
    page_ctx.eval(f"{p}.emptyAction.run(CON);")
    assert js(page_ctx, "CALLS") == [["goView", "platform", "providers", "llm"]]
    assert page_ctx.eval(f"typeof {p}.open") == "function", "a profile still opens in its own overlay"


def test_the_profiles_page_draws_aggregate_and_its_empty_state_points_to_providers(page_ctx) -> None:
    page_ctx.eval("CON.view = { name: 'platform', nav: 'profiles', sub: null }; ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'profiles' });")
    assert page_ctx.eval('MR.find("nv-plat-create").props.children') == "+ Aggregate", "the header's one action"
    assert "New profile" not in js(page_ctx, "MR.texts()"), "no single-profile create"
    assert "No LLM profiles yet. Register an LLM provider to import its models." in js(page_ctx, "MR.texts()")
    assert js(page_ctx, 'MR.subtree("nv-plat-empty").filter(function (e) { return e.type === "button"; }).length') == 1, "the empty state offers the way out, not a second create"
    page_ctx.eval('MR.click("nv-plat-empty-action")')
    assert js(page_ctx, "CALLS") == [["goView", "platform", "providers", "llm"]]
    page_ctx.eval("window.MP_ProfileModal = function (p) { return React.createElement('div', { 'data-testid': 'mp-modal', 'data-existing': p.existing && p.existing.id }); };"
                  "ITEMS = [{ id: 'p1', provider_id: 'llm-1', model_name: 'm' }]; MR.rerender();")
    assert page_ctx.eval('MR.find("mp-modal") === null')
    page_ctx.eval('MR.click("nv-plat-create")')
    assert page_ctx.eval('MR.find("mp-modal") !== null && MR.find("mp-modal").props["data-existing"] === null'), "+ Aggregate hosts the profile overlay with no profile"
    page_ctx.eval('MR.click("pk-card-open:p1")')
    assert page_ctx.eval('MR.find("mp-modal").props["data-existing"]') == "p1", "Open still hosts the profile's own form (NV_ProfileModalHost)"
