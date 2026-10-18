"""Every generic Platform page draws through the platform kit (Platform revamp Plan 01, Task 10; design 5, 13.5 and 20 Q1).

``NV_PlatListPage`` used to draw its own header, card and pager: the filter was 130px wide, the create button was ``New ...``, the pager sat below the grid and showed only
past six cards, the grid had a 1020px cap and Delete was a link in every card. It now draws the kit's: ``PK_PageHeader`` (the title and its count; the filter, the pager and
the page's one compact action, in that order), a full-width ``PK_CardGrid`` of ``PK_Card`` (twelve to a page), and Delete in the card's "⋯" menu with the prompt it always had.

The page runs here for real (``tests/ui/_platform_page_harness.py``: the kit and ``nv-platform.jsx``, transpiled, with the console stubbed). Its CSS is read from ``ui/styles.css``.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.ui._platform_page_harness import PLATFORM, platform_navs, platform_page_context
from tests.ui.test_platform_kit_page import CSS, css_body, declarations


# The approvals page mounts the decisions audit under its cards, which reads three resources of its own. The harness answers every resource with a new object on each render, and the audit's pending
# fetcher reports its answer from an effect keyed on that object, so it would re-render for ever; the real hook hands back the same object until the data changes. These keys get a steady empty answer.
QUIET_AUDIT = """
(function () {
  var inner = window.primerApi.useResource, QUIET = { items: [] };
  window.primerApi.useResource = function (key, fn, opts) {
    if (key.indexOf("nv-plat:audit-") === 0 || key === "nv-plat:approval-records") return { data: QUIET, loading: false, error: null, degraded: false, refetch: function () {} };
    return inner(key, fn, opts);
  };
})();
"""


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def page_ctx():
    ctx = platform_page_context(prelude_extra=QUIET_AUDIT)
    try:
        yield ctx
    finally:
        ctx.close()


def _agents(ctx, n: int, nav: str = "agents") -> None:
    """``n`` rows a0, a1, ... on the page of ``nav``, mounted."""
    ctx.eval(f"ITEMS = []; for (var i = 0; i < {n}; i++) ITEMS.push({{ id: 'a' + i }}); MR.mount(NV_PlatListPage, {{ nav: {json.dumps(nav)} }});")


def _ids(ctx, prefix: str = "pk-card:") -> list[str]:
    return [t[len(prefix):] for t in js(ctx, f"MR.findAll({json.dumps(prefix)}).map(function (e) {{ return e.props['data-testid']; }})")]


# ---- the header, the grid, the pager -------------------------------------------------------------------------------------------------------------


def test_the_header_holds_filter_pager_and_compact_action_and_the_grid_12_cards(page_ctx) -> None:
    _agents(page_ctx, 13)
    ids = [e.get("testid") for e in js(page_ctx, 'MR.subtree("pk-header")') if e.get("testid")]
    assert ids.index("nv-plat-filter") < ids.index("pk-pager") < ids.index("nv-plat-create")
    assert page_ctx.eval("MR.find('nv-plat-create').props.children") == "+ Agent"
    assert page_ctx.eval("MR.findAll('pk-card:').length") == 12
    assert "1-12 of 13" in js(page_ctx, "MR.texts()")
    assert "13 agents" in js(page_ctx, "MR.texts()"), "the count keeps the page's noun"


def test_the_page_title_is_the_headers_heading_and_the_filter_is_named(page_ctx) -> None:
    _agents(page_ctx, 1)
    assert page_ctx.eval('MR.find("pk-header").props.children[0].type') == "h2"
    assert "Agents" in js(page_ctx, "MR.texts()")
    assert page_ctx.eval('MR.find("nv-plat-filter").props["aria-label"]') == "Filter agents"


def test_page_two_holds_the_thirteenth_card_and_a_filter_returns_to_page_one(page_ctx) -> None:
    _agents(page_ctx, 13)
    page_ctx.eval('MR.click("pk-pager-next")')
    assert _ids(page_ctx) == ["a12"]
    assert "13-13 of 13" in js(page_ctx, "MR.texts()")
    assert page_ctx.eval('MR.find("pk-pager-next").props.disabled') is True and page_ctx.eval('MR.find("pk-pager-prev").props.disabled') is False
    page_ctx.eval('MR.find("nv-plat-filter").props.onChange({ target: { value: "a1" } }); MR.rerender();')
    assert _ids(page_ctx) == ["a1", "a10", "a11", "a12"], "a filter typed on page 2 lands on page 1 of the matches"
    assert "1-4 of 4" in js(page_ctx, "MR.texts()") and "4 of 13 agents" in js(page_ctx, "MR.texts()")


def test_a_list_that_shrinks_under_the_open_page_lands_on_its_last_page(page_ctx) -> None:
    """Review Focus 4: rows deleted elsewhere (another tab, the API) while the operator is on page 3: never an empty page or a range like 25-24 of 13."""
    _agents(page_ctx, 30)
    page_ctx.eval('MR.click("pk-pager-next"); MR.click("pk-pager-next");')
    assert len(_ids(page_ctx)) == 6 and "25-30 of 30" in js(page_ctx, "MR.texts()")
    page_ctx.eval("ITEMS = ITEMS.slice(0, 13); MR.rerender();")
    assert _ids(page_ctx) == ["a12"]
    assert "13-13 of 13" in js(page_ctx, "MR.texts()")
    page_ctx.eval("ITEMS = []; MR.rerender();")
    assert _ids(page_ctx) == [] and "0 of 0" in js(page_ctx, "MR.texts()")


def test_there_is_no_bottom_pager_and_no_old_card(page_ctx) -> None:
    _agents(page_ctx, 13)
    tree = js(page_ctx, 'MR.subtree("nv-plat-page:agents")')
    classes = [c for e in tree for c in e["className"].split()]
    assert not [c for c in classes if c.startswith(("nv-pager", "nv-pcard", "nv-fact", "nv-plat-head"))], "the page draws the kit's header and cards only"
    assert classes.count("pk-pager") == 1, "one pager, in the header, beside the filter"
    assert page_ctx.eval("MR.findAll('nv-pcard').length") == 0 and page_ctx.eval("MR.find('nv-plat-count') === null")


def test_the_card_is_opened_and_titled_from_the_view_model(page_ctx) -> None:
    _agents(page_ctx, 2)
    assert page_ctx.eval('MR.find("pk-card-open:a1").props["aria-label"]') == "Open a1"
    page_ctx.eval('MR.click("pk-card-open:a1")')
    assert ["openOverlay", "agents", None, "a1"] in js(page_ctx, "CALLS")


# ---- Delete: in the card's menu, with the prompt it always had ----------------------------------------------------------------------------------


def test_delete_lives_in_the_card_menu_and_keeps_todays_prompt(page_ctx) -> None:
    page_ctx.eval("ITEMS = [{ id: 'a1' }]; CONFIRM = false; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    assert page_ctx.eval("MR.find('nv-plat-delete:a1') === null"), "no Delete in the card itself"
    page_ctx.eval("MR.click('pk-card-menu:a1'); MR.click('nv-plat-delete:a1');")
    expected = js(page_ctx, "NV_deleteConfirm('agents', { id: 'a1' })")
    assert js(page_ctx, "DIALOGS[0]") == {"title": expected["title"], "message": expected["message"], "danger": True}


def test_confirming_the_delete_deletes_the_row_says_so_and_refreshes_the_list(page_ctx) -> None:
    page_ctx.eval("ITEMS = [{ id: 'a1' }]; CONFIRM = true; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    page_ctx.eval("MR.click('pk-card-menu:a1'); MR.click('nv-plat-delete:a1');")
    assert js(page_ctx, "FETCHES") == [["DELETE", "/agents/a1", None]]
    assert ["toast", "Deleted a1"] in [c[:2] for c in js(page_ctx, "CALLS")]
    assert page_ctx.eval("REFETCHES") == 1


def test_a_setup_agents_menu_prompt_names_the_consequence(page_ctx) -> None:
    """The prompt text is the entity's own (NV_deleteConfirm); moving Delete into the menu must not return to the generic sentence."""
    page_ctx.eval("ITEMS = [{ id: 'operator' }]; CONFIRM = false; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    page_ctx.eval("MR.click('pk-card-menu:operator'); MR.click('nv-plat-delete:operator');")
    assert "not set up" in js(page_ctx, "DIALOGS[0].message")


def test_a_built_in_toolset_has_no_menu(page_ctx) -> None:
    page_ctx.eval("ITEMS = [{ id: 'web', builtin: true }, { id: 'mine' }]; MR.mount(NV_PlatListPage, { nav: 'toolsets' });")
    assert page_ctx.eval("MR.find('pk-card-menu:web') === null"), "a built-in ships with the platform: it cannot be deleted, so its card has nothing to put in a menu"
    assert page_ctx.eval("!!MR.find('pk-card-menu:mine')")


@pytest.mark.parametrize("nav", ["profiles", "agents", "graphs", "collections", "triggers", "channels", "harnesses", "services", "approvals"])
def test_every_deletable_entry_gets_one_danger_item_in_its_menu(page_ctx, nav) -> None:
    page_ctx.eval(f"ITEMS = [{{ id: 'r1' }}]; MR.mount(NV_PlatListPage, {{ nav: {json.dumps(nav)} }}); MR.click('pk-card-menu:r1');")
    assert page_ctx.eval("MR.find('nv-plat-delete:r1').props['data-danger']") == "true"
    assert page_ctx.eval("MR.find('nv-plat-delete:r1').props.children") == "Delete"
    assert page_ctx.eval("MR.findAll('nv-plat-delete:').length") == 1


def test_the_tool_catalogue_has_no_menu_because_it_is_read_only(page_ctx) -> None:
    page_ctx.eval("ITEMS = [{ id: 'web__search' }]; MR.mount(NV_PlatListPage, { nav: 'tools' });")
    assert page_ctx.eval("!!MR.find('pk-card:web__search')") and page_ctx.eval("MR.find('pk-card-menu:web__search') === null")


# ---- the states -----------------------------------------------------------------------------------------------------------------------------------


def test_loading_error_and_empty_states(page_ctx) -> None:
    page_ctx.eval("ITEMS = null; LOADING = true; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    assert page_ctx.eval("!!MR.find('nv-plat-loading')") and "Loading…" in js(page_ctx, "MR.texts()")
    assert page_ctx.eval("MR.find('nv-plat-empty') === null && MR.find('nv-plat-error') === null"), "a list on its way is not an empty list"
    page_ctx.eval("LOADING = false; ERROR = { detail: 'the list could not be read' }; MR.rerender();")
    assert page_ctx.eval("!!MR.find('nv-plat-error')") and "the list could not be read" in js(page_ctx, "MR.texts()")
    assert page_ctx.eval("MR.find('nv-plat-loading') === null && MR.find('nv-plat-empty') === null"), "an error is not an empty list either"
    page_ctx.eval("ERROR = null; ITEMS = []; MR.rerender();")
    assert page_ctx.eval("!!MR.find('nv-plat-empty')") and "No agents yet." in js(page_ctx, "MR.texts()")
    assert page_ctx.eval("MR.find('nv-plat-loading') === null && MR.find('nv-plat-error') === null")


def test_the_loading_line_is_a_status_and_the_error_keeps_the_form_error_class(page_ctx) -> None:
    page_ctx.eval("ITEMS = null; LOADING = true; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    assert page_ctx.eval("MR.find('nv-plat-loading').props.role") == "status"
    page_ctx.eval("LOADING = false; ERROR = { message: 'down' }; MR.rerender();")
    assert page_ctx.eval("MR.find('nv-plat-error').props.className") == "nv-form-error"


def test_a_filter_that_hides_every_card_says_so_and_clear_filter_brings_them_back(page_ctx) -> None:
    _agents(page_ctx, 3)
    page_ctx.eval('MR.find("nv-plat-filter").props.onChange({ target: { value: "zzz" } }); MR.rerender();')
    assert 'No agents match "zzz".' in js(page_ctx, "MR.texts()") and "0 of 3 agents" in js(page_ctx, "MR.texts()")
    page_ctx.eval('MR.click("nv-plat-clear-filter")')
    assert len(_ids(page_ctx)) == 3 and page_ctx.eval('MR.find("nv-plat-filter").props.value') == ""


def test_the_empty_state_offers_the_pages_own_compact_create(page_ctx) -> None:
    page_ctx.eval("ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'graphs' });")
    buttons = js(page_ctx, 'MR.subtree("nv-plat-empty").filter(function (e) { return e.type === "button"; }).length')
    assert buttons == 1 and js(page_ctx, "MR.texts()").count("+ Graph") == 2, "the header's action and the empty state's"


# ---- the secondary control and the action ----------------------------------------------------------------------------------------------------------


def test_the_extra_control_is_a_kit_secondary_button_left_of_the_filter(page_ctx) -> None:
    page_ctx.eval("ITEMS = [{ id: 'c1' }]; CON.view = { name: 'platform', nav: 'channels', sub: null }; MR.mount(NV_PlatListPage, { nav: 'channels' });")
    ids = [e.get("testid") for e in js(page_ctx, 'MR.subtree("pk-header")') if e.get("testid")]
    assert ids.index("nv-plat-extra") < ids.index("nv-plat-filter") < ids.index("pk-pager") < ids.index("nv-plat-create")
    assert page_ctx.eval("MR.find('nv-plat-extra').props.className") == "pk-extra" and page_ctx.eval("MR.find('nv-plat-extra').props.children") == "Rules"
    page_ctx.eval("MR.click('nv-plat-extra')")
    assert ["openOverlay", "channels", "rules", None] in js(page_ctx, "CALLS")


def test_the_action_runs_the_pages_create(page_ctx) -> None:
    page_ctx.eval("window.GR_NewGraphModal = function () { return React.createElement('div', { 'data-testid': 'graph-dialog' }); };"
                  "ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'graphs' });")
    assert page_ctx.eval("MR.find('graph-dialog') === null")
    page_ctx.eval("MR.click('nv-plat-create')")
    assert page_ctx.eval("!!MR.find('graph-dialog')"), "the create form is hosted on the page"


def test_a_page_without_a_create_draws_no_action(page_ctx) -> None:
    page_ctx.eval("ITEMS = []; MR.mount(NV_PlatListPage, { nav: 'tools' });")
    assert page_ctx.eval("MR.find('nv-plat-create') === null") and page_ctx.eval("!!MR.find('nv-plat-filter')")


# ---- the helpers --------------------------------------------------------------------------------------------------------------------------------


def test_card_props_map_the_view_model(page_ctx) -> None:
    props = js(page_ctx, 'NV_platCardProps("graphs", { id: "g", name: "g-name", sub: "s", badge: "b", facts: ["f"] })')
    assert props == {"id": "g", "title": "g-name", "subtitle": "s", "badge": "b", "facts": ["f"], "icon": {"path": page_ctx.eval("NV_PLAT_ICONS.graphs")}}
    own = js(page_ctx, 'NV_platCardProps("graphs", { id: "g", name: "g", sub: "", facts: [], icon: { path: "M1 1", tone: "var(--teal)" } })')
    assert own["icon"] == {"path": "M1 1", "tone": "var(--teal)"}, "the entry's own icon wins over its page's"
    assert page_ctx.eval('var VM = { id: "t", name: "t", sub: "", facts: [], status: { label: "off", tone: "warn", onClick: rec("chip"), testid: "t-chip" } };'
                         'NV_platCardProps("toolsets", VM).status === VM.status'), "an actionable status passes through (seam I-8)"


def test_every_nav_has_an_icon_for_its_cards() -> None:
    ctx = platform_page_context()
    try:
        for nav in platform_navs():
            assert ctx.eval(f"typeof NV_PLAT_ICONS[{json.dumps(nav)}]") == "string", nav
    finally:
        ctx.close()


def test_the_filter_text_is_name_type_line_and_id(page_ctx) -> None:
    assert page_ctx.eval('NV_platFilterText({ id: "web__search", name: "search", sub: "Find pages" })') == "search Find pages web__search"
    assert page_ctx.eval('NV_platFilterText({ id: "a1", name: "a1", sub: "" })') == "a1 a1", "an empty type line is left out"


def test_every_entry_has_a_short_action_label(page_ctx) -> None:
    labels = {nav: page_ctx.eval(f"NV_PLAT_PAGES[{json.dumps(nav)}].createLabel || null") for nav in platform_navs()}
    assert {nav for nav, label in labels.items() if label} == {"profiles", "agents", "graphs", "toolsets", "collections", "triggers", "channels", "harnesses", "services", "approvals"}
    for nav, label in labels.items():
        assert label is None or re.fullmatch(r"\+ [A-Z][a-z]+", label), f"{nav}: {label!r} is not a short '+ Thing' label (design 5.1)"
    assert page_ctx.eval("NV_PLAT_PAGES.channels.extraNav.label") == "Rules" and page_ctx.eval("NV_PLAT_PAGES.harnesses.extraNav.label") == "Build outbound"


def test_the_page_size_is_twelve_and_the_slicing_markers_are_still_where_the_other_tests_cut() -> None:
    assert re.search(r"^var NV_PLAT_PAGE_SIZE = 12;$", PLATFORM.read_text(encoding="utf-8"), re.M)
    src = PLATFORM.read_text(encoding="utf-8")
    assert src.index("function NV_addChannelProvider(") < src.index("function NV_platCardProps(") < src.index("function NV_platFilterText(") < src.index("// Per-entity page config.")
    assert src.index("function NV_fact(") < src.index("// Per-entity page config.") < src.index("var NV_PLAT_PAGE_SIZE")
    jsx_free = src[src.index("function NV_fact("):src.index("var NV_PLAT_PAGE_SIZE")]
    assert "</" not in jsx_free and "/>" not in jsx_free, "the stretch other tests evaluate without transpiling stays JSX-free"


# ---- the CSS ------------------------------------------------------------------------------------------------------------------------------------


def test_the_platform_wrap_has_no_width_cap() -> None:
    wrap = declarations(css_body(".nv-plat-wrap"))
    assert "max-width" not in wrap and "margin" not in wrap, "a Platform page is as wide as its pane (design 5.3 and 13.5)"
    assert set(wrap) == {"padding"}, "the frame keeps only its padding"


def test_the_empty_state_has_the_cards_radius() -> None:
    assert declarations(css_body(".nv-plat-empty")).get("border-radius") == "var(--r-12)"


def test_the_loading_line_is_styled() -> None:
    d = declarations(css_body(".nv-plat-state"))
    assert d.get("text-align") == "center" and d.get("color") == "var(--text-4)"


def test_the_old_header_card_and_pager_rules_are_gone_and_the_system_title_is_not() -> None:
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    dead = re.findall(r"\.nv-(?:plat-head|plat-count|plat-filter|fam-pill|pcard|fact-|pager)[\w-]*", css)
    assert not dead, f"rules of the markup the kit replaced are still in the stylesheet: {sorted(set(dead))}"
    assert ".nv-plat-title" in css, "the System pages draw their title with this class"
