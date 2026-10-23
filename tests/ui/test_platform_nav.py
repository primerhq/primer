"""The Platform sidebar and the page it opens (Platform revamp Plan 01, Tasks 6 and 7; seams I-1, I-2 and I-7).

The real ``NV_PlatNav`` and ``NV_PlatPage`` run here on the page harness (``tests/ui/_platform_page_harness.py``), with the real grammar of ``ui/foundation/shell-url.js`` loaded
beside them (the sidebar reads its type list from ``window.SH_VIEW_SUBS``, so a stand-in list would test the stand-in) and a recording ``window.ProviderCatalog`` where the real one
would mount.

Task 6 (this file's first half): the Providers row has one sub-entry per provider type, always visible; the entry of the open type is the current one and the Providers row is current
only on All; a click goes to the type; the page mounts the catalog at the type.

Task 7 (the second half): the sidebar's groups are the design's (section 4.1) with no Workspaces row; ``web`` and ``templates`` are custom pages that exist only while the component that
draws them is loaded (``window.WEB_SettingsPage``, ``window.WT_TemplatesPage``: Plans 04 and 08 ship them) and mount inside the Platform page frame; a link to a custom page whose component is
not loaded renders Providers, not a blank page; the page titles read "LLM Profiles" and "Channels".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._platform_page_harness import PLATFORM, platform_navs, platform_page_context

ROOT = Path(__file__).resolve().parents[2]
SHELL_URL = (ROOT / "ui" / "foundation" / "shell-url.js").read_text(encoding="utf-8")

TYPES = ["llm", "embedding", "ssp", "cross_encoder", "workspace", "web_search", "channel", "artifact_storage", "tts", "stt"]
LABELS = ["LLM", "Embedding", "Semantic Search Storage", "Cross Encoding", "Workspace", "Web Search", "Channels", "Artifact Store", "TTS", "ASR"]

# A recording stand-in where window.ProviderCatalog mounts: what it was asked to open is what the page decided, and MOUNTS / UNMOUNTS count how often an instance of it
# came and went (the real catalog keeps its class and its open instance in state, which a new instance starts empty).
CATALOG = """
var CATALOG_SEEN = [], CATALOG_PROPS = null, MOUNTS = 0, UNMOUNTS = 0;
window.ProviderCatalog = function (p) {
  CATALOG_PROPS = p;
  React.useEffect(function () { MOUNTS += 1; return function () { UNMOUNTS += 1; }; }, []);
  CATALOG_SEEN.push({ initialClass: p.initialClass, initialInstanceId: p.initialInstanceId === null ? "null" : typeof p.initialInstanceId, onNavigate: typeof p.onNavigate });
  return React.createElement("div", { "data-testid": "catalog-stand-in", "data-class": p.initialClass });
};
"""


# The text of an element tree, for the label of a nav row; and a stand-in for a custom page's component (what a later plan ships on window).
TEXT = """
function TXT(n) {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(TXT).join("");
  return n.__el ? TXT(n.children) : "";
}
var STAND_IN_PROPS = [];
function STAND_IN(id) {
  return function (props) { STAND_IN_PROPS.push(Object.keys(props || {})); return React.createElement("div", { "data-testid": id }); };
}
"""

GROUPS = ["Intelligence", "Workbench", "Automation", "Governance"]
SIDEBAR = [
    ("Intelligence", ["providers", "profiles", "agents", "graphs"]),
    ("Workbench", ["templates", "toolsets", "tools", "collections", "web"]),
    ("Automation", ["triggers", "channels", "harnesses", "services"]),
    ("Governance", ["approvals"]),
]


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def nav_ctx():
    ctx = platform_page_context(prelude_extra=SHELL_URL + CATALOG + TEXT)
    try:
        yield ctx
    finally:
        ctx.close()


def _sub_ids(ctx) -> list[str]:
    return js(ctx, 'MR.findAll("nv-plat-sub:").map(function (e) { return e.props["data-testid"].slice("nv-plat-sub:".length); })')


# ---- Task 6: the Providers sub-entries ------------------------------------------------------------------------------------------------


def test_the_providers_row_has_one_sub_entry_per_type_in_order(nav_ctx) -> None:
    nav_ctx.eval("MR.mount(NV_PlatNav, {});")
    assert _sub_ids(nav_ctx) == TYPES + ["register"]
    assert js(nav_ctx, 'MR.findAll("nv-plat-sub:").map(function (e) { return e.props.children; })') == LABELS + ["+ Register"]


def test_the_sub_entries_sit_in_one_labelled_group_under_the_providers_row(nav_ctx) -> None:
    nav_ctx.eval("MR.mount(NV_PlatNav, {});")
    assert js(nav_ctx, 'MR.find("nv-plat-subs").props.role') == "group"
    assert js(nav_ctx, 'MR.find("nv-plat-subs").props["aria-label"]') == "Provider types"
    assert js(nav_ctx, 'MR.find("nv-plat-subs").props.className') == "nv-plat-subs"
    order = js(nav_ctx, 'MR.subtree("nv-plat-nav").map(function (e) { return e.testid; }).filter(Boolean)')
    providers = order.index("nv-plat-row:providers")
    assert order[providers + 1] == "nv-plat-subs"
    assert order[providers + 2:providers + 2 + len(TYPES)] == ["nv-plat-sub:" + t for t in TYPES], "the types follow the Providers row, before the next row"
    assert order[providers + 2 + len(TYPES)] == "nv-plat-sub:register"
    assert order[providers + 3 + len(TYPES)] == "nv-plat-row:profiles"


def test_a_sub_entry_is_a_button_of_the_sub_class(nav_ctx) -> None:
    nav_ctx.eval("MR.mount(NV_PlatNav, {});")
    assert nav_ctx.eval('MR.find("nv-plat-sub:tts").type') == "button"
    assert nav_ctx.eval('MR.find("nv-plat-sub:tts").props.type') == "button"
    assert nav_ctx.eval('MR.find("nv-plat-sub:tts").props.className') == "nv-plat-sub"


def test_a_sub_entry_is_current_and_the_providers_row_is_not(nav_ctx) -> None:
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "tts" }; MR.mount(NV_PlatNav, {});')
    assert nav_ctx.eval('MR.find("nv-plat-sub:tts").props["aria-current"]') == "page"
    assert nav_ctx.eval('MR.find("nv-plat-sub:tts").props["data-active"]') == "true"
    assert nav_ctx.eval('MR.find("nv-plat-row:providers").props["aria-current"] === undefined')
    assert nav_ctx.eval('MR.find("nv-plat-row:providers").props["data-active"]') == "false"
    assert nav_ctx.eval('MR.find("nv-plat-sub:llm").props["aria-current"] === undefined'), "only one type is current"


def test_the_providers_row_is_current_on_all(nav_ctx) -> None:
    nav_ctx.eval('CON.view = { name: "platform", nav: null, sub: null }; MR.mount(NV_PlatNav, {});')
    assert nav_ctx.eval('MR.find("nv-plat-row:providers").props["aria-current"]') == "page", "no nav at all is Providers, All"
    assert all(nav_ctx.eval(f'MR.find("nv-plat-sub:{t}").props["aria-current"] === undefined') for t in TYPES)
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: null }; MR.rerender();')
    assert nav_ctx.eval('MR.find("nv-plat-row:providers").props["aria-current"]') == "page"


def test_another_page_marks_its_own_row_only(nav_ctx) -> None:
    nav_ctx.eval('CON.view = { name: "platform", nav: "agents", sub: null }; MR.mount(NV_PlatNav, {});')
    assert nav_ctx.eval('MR.find("nv-plat-row:agents").props["aria-current"]') == "page"
    assert nav_ctx.eval('MR.find("nv-plat-row:providers").props["aria-current"] === undefined')
    assert all(nav_ctx.eval(f'MR.find("nv-plat-sub:{t}").props["aria-current"] === undefined') for t in TYPES)


def test_a_type_on_a_page_that_has_none_marks_nothing_as_a_type(nav_ctx) -> None:
    """A view that carries a type on a nav without types (a hand-built one; the grammar drops it) is just the nav."""
    nav_ctx.eval('CON.view = { name: "platform", nav: "agents", sub: "llm" }; MR.mount(NV_PlatNav, {});')
    assert nav_ctx.eval('MR.find("nv-plat-row:agents").props["aria-current"]') == "page"
    assert nav_ctx.eval('MR.find("nv-plat-sub:llm").props["aria-current"] === undefined')


def test_clicking_a_sub_entry_goes_to_its_type_and_the_row_to_all(nav_ctx) -> None:
    nav_ctx.eval('CON.view = { name: "platform", nav: "agents", sub: null }; MR.mount(NV_PlatNav, {}); MR.click("nv-plat-sub:llm");')
    assert js(nav_ctx, "CALLS") == [["goView", "platform", "providers", "llm"]]
    nav_ctx.eval('MR.click("nv-plat-row:providers");')
    assert js(nav_ctx, "CALLS")[1] == ["goView", "platform", "providers"], "the Providers row is All: no type"


def test_every_other_row_still_goes_to_its_nav(nav_ctx) -> None:
    nav_ctx.eval('MR.mount(NV_PlatNav, {}); MR.click("nv-plat-row:agents");')
    assert js(nav_ctx, "CALLS") == [["goView", "platform", "agents"]]


def test_the_sub_entries_follow_the_grammar_not_a_list_of_their_own() -> None:
    """Without the grammar loaded there is nothing to list: the sidebar draws the Providers row and no group, rather than a stale copy of the types."""
    ctx = platform_page_context(prelude_extra=CATALOG)
    try:
        ctx.eval("MR.mount(NV_PlatNav, {});")
        assert js(ctx, 'MR.findAll("nv-plat-sub:").length') == 0
        assert ctx.eval('!!MR.find("nv-plat-row:providers")')
        assert js(ctx, "NV_platProviderTypes()") == []
    finally:
        ctx.close()


def test_the_type_list_and_labels_are_the_grammars(nav_ctx) -> None:
    assert js(nav_ctx, "NV_platProviderTypes()") == TYPES
    assert js(nav_ctx, "NV_PROVIDER_TYPE_LABELS") == dict(zip(TYPES, LABELS))
    assert js(nav_ctx, "NV_platProviderTypes().every(function (t) { return typeof NV_PROVIDER_TYPE_LABELS[t] === 'string'; })") is True


def test_the_helpers_are_on_window(nav_ctx) -> None:
    for name in ("NV_PROVIDER_TYPE_LABELS", "NV_platProviderTypes", "NV_platNavCurrent", "NV_providersNavigate"):
        assert nav_ctx.eval(f"typeof window.{name} !== 'undefined'"), name


@pytest.mark.parametrize(
    "view,expected",
    [
        ('{ name: "platform", nav: null, sub: null }', "providers"),
        ('{ name: "platform", nav: "providers", sub: null }', "providers"),
        ('{ name: "platform", nav: "providers", sub: "tts" }', "providers:tts"),
        ('{ name: "platform", nav: "providers", sub: "bogus" }', "providers"),
        ('{ name: "platform", nav: "agents", sub: null }', "agents"),
        ('{ name: "platform", nav: "agents", sub: "llm" }', "agents"),
        ("null", "providers"),
    ],
)
def test_the_current_nav_is_the_nav_or_its_type(nav_ctx, view: str, expected: str) -> None:
    assert nav_ctx.eval(f"NV_platNavCurrent({view})") == expected


# ---- Task 6: the page mounts the catalog at the type ------------------------------------------------------------------------------


def test_the_page_mounts_the_catalog_at_the_type(nav_ctx) -> None:
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "tts" }; MR.mount(NV_PlatPage, {});')
    assert js(nav_ctx, "CATALOG_SEEN[CATALOG_SEEN.length - 1]") == {"initialClass": "tts", "initialInstanceId": "null", "onNavigate": "function"}
    assert nav_ctx.eval('MR.find("catalog-stand-in").props["data-class"]') == "tts"
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: null }; MR.rerender();')
    assert js(nav_ctx, "CATALOG_SEEN[CATALOG_SEEN.length - 1]")["initialClass"] == "all"
    nav_ctx.eval('CON.view = { name: "platform", nav: null, sub: null }; MR.rerender();')
    assert js(nav_ctx, "CATALOG_SEEN[CATALOG_SEEN.length - 1]")["initialClass"] == "all", "no nav is Providers, All"


def test_a_move_the_sidebar_makes_starts_the_catalog_over(nav_ctx) -> None:
    """The catalog owns its class and its open instance in state of its own and only seeds them from its props, so a sidebar click (a view object the catalog did not
    make) remounts it at the type the address names: otherwise a vector store's detail page was asked for the LLM provider that was open under the previous type."""
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "llm" }; MR.mount(NV_PlatPage, {});')
    assert js(nav_ctx, "[MOUNTS, UNMOUNTS]") == [1, 0]
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "tts" }; MR.rerender();')
    assert js(nav_ctx, "[MOUNTS, UNMOUNTS]") == [2, 1]
    assert js(nav_ctx, "CATALOG_SEEN[CATALOG_SEEN.length - 1].initialClass") == "tts"


def test_a_click_on_the_type_that_is_already_open_starts_it_over_too(nav_ctx) -> None:
    """The sidebar entry of the open type is a way back to that type's list from one of its instances: a new view object with the same type."""
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "ssp" }; MR.mount(NV_PlatPage, {});')
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "ssp" }; MR.rerender();')
    assert js(nav_ctx, "[MOUNTS, UNMOUNTS]") == [2, 1]


def test_a_move_the_catalog_made_itself_keeps_it_mounted(nav_ctx) -> None:
    """A card click on All selects the card's class AND opens its edit form in one call; a remount would close the form it just opened."""
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: null }; MR.mount(NV_PlatPage, {});')
    nav_ctx.eval('CATALOG_PROPS.onNavigate({ kind: "provider-class", classKey: "llm" });')
    assert js(nav_ctx, "CALLS") == [["goView", "platform", "providers", "llm"]]
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "llm" }; MR.rerender();')    # what the shell's goView does
    assert js(nav_ctx, "[MOUNTS, UNMOUNTS]") == [1, 0]
    assert js(nav_ctx, "CATALOG_SEEN[CATALOG_SEEN.length - 1].initialClass") == "llm", "the catalog is told the type it is already on"
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "tts" }; MR.rerender();')
    assert js(nav_ctx, "[MOUNTS, UNMOUNTS]") == [2, 1], "the next move is the sidebar's again, and the catalog's own move does not excuse it"


def test_a_class_with_no_sidebar_entry_neither_moves_the_address_nor_excuses_the_next_move(nav_ctx) -> None:
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "llm" }; MR.mount(NV_PlatPage, {});')
    nav_ctx.eval('CATALOG_PROPS.onNavigate({ kind: "provider-class", classKey: "model_profile" });')
    assert js(nav_ctx, "CALLS") == []
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "tts" }; MR.rerender();')
    assert js(nav_ctx, "[MOUNTS, UNMOUNTS]") == [2, 1], "nothing moved the address for the catalog, so this move is the sidebar's"


def test_a_render_with_the_same_view_keeps_the_catalog(nav_ctx) -> None:
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "tts" }; MR.mount(NV_PlatPage, {}); MR.rerender(); MR.rerender({});')
    assert js(nav_ctx, "[MOUNTS, UNMOUNTS]") == [1, 0]


def test_the_page_frame_is_the_providers_page(nav_ctx) -> None:
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "tts" }; MR.mount(NV_PlatPage, {});')
    assert nav_ctx.eval('!!MR.find("nv-plat-page:providers")')


def test_the_catalogs_navigation_goes_through_the_console(nav_ctx) -> None:
    """``onNavigate`` is the page's ``NV_providersNavigate``: a chip click inside the catalog moves the address, not just the catalog's own state."""
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: null }; MR.mount(NV_PlatPage, {});')
    nav_ctx.eval('CATALOG_PROPS.onNavigate({ kind: "provider-class", classKey: "embedding" });')
    assert js(nav_ctx, "CALLS") == [["goView", "platform", "providers", "embedding"]]
    nav_ctx.eval('CATALOG_PROPS.onNavigate({ kind: "provider-instance", classKey: "llm", id: "p1" });')
    assert len(js(nav_ctx, "CALLS")) == 1, "an instance does not move the address"


def test_providers_navigate_follows_sidebar_types_only(nav_ctx) -> None:
    assert nav_ctx.eval('NV_providersNavigate(CON, { kind: "provider-class", classKey: "embedding" })') is True
    assert js(nav_ctx, "CALLS") == [["goView", "platform", "providers", "embedding"]]
    nav_ctx.eval("CALLS.length = 0;")
    for ref in ('{ kind: "provider-class", classKey: "web_fetch" }', '{ kind: "provider-class", classKey: "model_profile" }',
                '{ kind: "provider-instance", classKey: "llm", id: "p1" }', "null", "undefined", "{}"):
        assert nav_ctx.eval(f"NV_providersNavigate(CON, {ref})") is False, ref
    assert js(nav_ctx, "CALLS") == [], "the catalog's other chips and its instances do not move the address"


def test_the_address_does_not_move_to_the_type_it_is_already_on(nav_ctx) -> None:
    """A chip for the open type goes nowhere: no history entry for it."""
    nav_ctx.eval('CON.view = { name: "platform", nav: "providers", sub: "llm" };')
    assert nav_ctx.eval('NV_providersNavigate(CON, { kind: "provider-class", classKey: "llm" })') is False
    assert js(nav_ctx, "CALLS") == []
    assert nav_ctx.eval('NV_providersNavigate(CON, { kind: "provider-class", classKey: "tts" })') is True
    nav_ctx.eval('CON.view = { name: "platform", nav: "agents", sub: null }; CALLS.length = 0;')
    assert nav_ctx.eval('NV_providersNavigate(CON, { kind: "provider-class", classKey: "llm" })') is True, "from another page the same type is a move"


def test_new_channel_with_no_provider_goes_to_the_channel_type(nav_ctx) -> None:
    nav_ctx.eval("NV_addChannelProvider(CON);")
    assert js(nav_ctx, "CALLS") == [["goView", "platform", "providers", "channel"]]


# ---- Task 7: the groups, the custom pages and the titles ------------------------------------------------------------------------------


def _row_ids(ctx) -> list[str]:
    return js(ctx, 'MR.findAll("nv-plat-row:").map(function (e) { return e.props["data-testid"].slice("nv-plat-row:".length); })')


def _row_label(ctx, nav: str) -> str:
    return ctx.eval(f'TXT(MR.find("nv-plat-row:{nav}").children)')


LOAD_BOTH = "window.WEB_SettingsPage = STAND_IN('web-stand-in'); window.WT_TemplatesPage = STAND_IN('templates-stand-in');"


def test_the_sidebar_has_the_new_groups_and_no_workspaces_row(nav_ctx) -> None:
    nav_ctx.eval("MR.mount(NV_PlatNav, {});")
    pages = set(platform_navs())
    expected = [nav for nav in js(nav_ctx, "SH_VIEWS.platform") if nav == "providers" or nav in pages]
    assert _row_ids(nav_ctx) == expected, "one row per available id of the grammar, in the grammar's order"
    assert "workspaces" not in _row_ids(nav_ctx) and nav_ctx.eval('MR.find("nv-plat-row:workspaces") === null')
    texts = js(nav_ctx, "MR.texts()")
    assert [t for t in texts if t in GROUPS] == GROUPS


def test_the_groups_hold_the_rows_of_the_design(nav_ctx) -> None:
    """Design 4.1: Intelligence (Providers, LLM Profiles, Agents, Graphs), Workbench (Workspace templates, Toolsets, Tools, Collections, Web), Automation, Governance."""
    assert js(nav_ctx, "NV_PLAT_GROUPS") == [{"label": label, "ids": ids} for label, ids in SIDEBAR]
    flat = [nav for _, ids in SIDEBAR for nav in ids]
    assert js(nav_ctx, "SH_VIEWS.platform") == flat, "the sidebar's order is the grammar's order"
    nav_ctx.eval(LOAD_BOTH + " MR.mount(NV_PlatNav, {});")
    assert _row_ids(nav_ctx) == flat, "with both custom pages loaded every id of the grammar has its row"


def test_custom_rows_show_only_while_their_component_is_loaded(nav_ctx) -> None:
    nav_ctx.eval("MR.mount(NV_PlatNav, {});")
    assert nav_ctx.eval('MR.find("nv-plat-row:web") === null && MR.find("nv-plat-row:templates") === null'), "no placeholder row ships"
    nav_ctx.eval(LOAD_BOTH + " MR.rerender();")
    assert _row_label(nav_ctx, "web") == "Web" and _row_label(nav_ctx, "templates") == "Workspace templates"
    nav_ctx.eval("delete window.WEB_SettingsPage; MR.rerender();")
    assert nav_ctx.eval('MR.find("nv-plat-row:web") === null') and nav_ctx.eval('!!MR.find("nv-plat-row:templates")'), "each row follows its own component"


def test_a_component_that_is_not_a_function_does_not_make_its_row(nav_ctx) -> None:
    nav_ctx.eval('window.WEB_SettingsPage = { not: "a component" }; window.WT_TemplatesPage = null; MR.mount(NV_PlatNav, {});')
    assert nav_ctx.eval('MR.find("nv-plat-row:web") === null && MR.find("nv-plat-row:templates") === null')


def test_a_custom_row_goes_to_its_page_and_is_current_on_it(nav_ctx) -> None:
    nav_ctx.eval(LOAD_BOTH + ' CON.view = { name: "platform", nav: "web", sub: null }; MR.mount(NV_PlatNav, {});')
    assert nav_ctx.eval('MR.find("nv-plat-row:web").props["aria-current"]') == "page"
    assert nav_ctx.eval('MR.find("nv-plat-row:providers").props["aria-current"] === undefined')
    nav_ctx.eval('MR.click("nv-plat-row:templates");')
    assert js(nav_ctx, "CALLS") == [["goView", "platform", "templates"]]


def test_a_custom_page_mounts_its_component_in_the_page_frame(nav_ctx) -> None:
    nav_ctx.eval(LOAD_BOTH + ' CON.view = { name: "platform", nav: "web", sub: null }; MR.mount(NV_PlatPage, {});')
    assert nav_ctx.eval('MR.find("nv-plat-page:web").props.className') == "nv-plat-wrap"
    inside = js(nav_ctx, 'MR.subtree("nv-plat-page:web").map(function (e) { return e.testid; })')
    assert "web-stand-in" in inside and "templates-stand-in" not in inside
    assert js(nav_ctx, "STAND_IN_PROPS") == [[]], "the component reads the console itself and is given no props"
    nav_ctx.eval('CON.view = { name: "platform", nav: "templates", sub: null }; MR.rerender();')
    assert nav_ctx.eval('MR.find("nv-plat-page:web") === null') and nav_ctx.eval('!!MR.find("nv-plat-page:templates")')
    assert "templates-stand-in" in js(nav_ctx, 'MR.subtree("nv-plat-page:templates").map(function (e) { return e.testid; })')


def test_a_custom_page_link_with_no_component_falls_back_to_providers(nav_ctx) -> None:
    """Review Focus 5: an unloaded custom page lands on Providers and canonicalises the view without pushing history."""
    for nav in ("web", "templates"):
        nav_ctx.eval(f'CON.view = {{ name: "platform", nav: "{nav}", sub: null }}; CALLS.length = 0; CATALOG_SEEN.length = 0; MR.mount(NV_Platform, {{}});')
        assert js(nav_ctx, "CATALOG_SEEN[CATALOG_SEEN.length - 1].initialClass") == "all", nav
        assert nav_ctx.eval('!!MR.find("nv-plat-page:providers")') and nav_ctx.eval(f'MR.find("nv-plat-page:{nav}") === null'), nav
        assert nav_ctx.eval('MR.find("nv-plat-row:providers").props["aria-current"]') == "page", nav
        assert nav_ctx.eval(f'MR.find("nv-plat-row:{nav}") === null'), nav
        assert js(nav_ctx, "CALLS") == [["replaceView", "platform", "providers"]], nav


def test_a_retired_nav_in_the_state_falls_back_to_providers_too(nav_ctx) -> None:
    """The grammar no longer admits ``workspaces``, but a view object can still carry any id (a caller's mistake): the page shows Providers, not a blank list page."""
    nav_ctx.eval('CON.view = { name: "platform", nav: "workspaces", sub: null }; CATALOG_SEEN.length = 0; MR.mount(NV_Platform, {});')
    assert js(nav_ctx, "CATALOG_SEEN[CATALOG_SEEN.length - 1].initialClass") == "all"
    assert nav_ctx.eval('MR.find("nv-plat-row:providers").props["aria-current"]') == "page"


def test_the_labels_read_llm_profiles_and_channels(nav_ctx) -> None:
    nav_ctx.eval("MR.mount(NV_PlatNav, {});")
    assert _row_label(nav_ctx, "profiles") == "LLM Profiles"
    assert _row_label(nav_ctx, "channels") == "Channels"
    assert _row_label(nav_ctx, "providers") == "Providers"
    dash = chr(0x2014)
    assert not any(dash in text for text in js(nav_ctx, "MR.texts()")), "no rendered text of the sidebar carries the long dash"
    titles = js(nav_ctx, "Object.keys(NV_PLAT_PAGES).map(function (k) { return NV_PLAT_PAGES[k].title; })")
    assert not any(dash in title for title in titles), titles


def test_the_list_pages_draw_the_new_titles(nav_ctx) -> None:
    for nav, title in (("profiles", "LLM Profiles"), ("channels", "Channels")):
        nav_ctx.eval(f'ITEMS = []; MR.mount(NV_PlatListPage, {{ nav: "{nav}" }});')
        assert title in js(nav_ctx, "MR.texts()"), nav
        assert nav_ctx.eval('MR.find("nv-plat-filter").props["aria-label"]') == f"Filter {title.lower()}"


def test_the_workspaces_and_templates_pages_are_gone_from_the_table_and_the_page() -> None:
    """Design 14.1 and 14.2: Workspaces leaves the Platform view, and Workspace templates is a custom page (Plan 08's), so the generic entries and the JSON form that wiped template fields go."""
    keys = platform_navs()
    assert "workspaces" not in keys and "templates" not in keys
    src = PLATFORM.read_text(encoding="utf-8")
    assert "NV_TemplateModal" not in src and 'modal.kind === "template"' not in src
    assert "NV_deleteConfirm" in src and 'nav === "workspaces"' in src, "the pure delete prompt for a workspace is kept (test_platform_delete_confirm.py)"


def test_the_custom_pages_and_their_labels_are_the_seam(nav_ctx) -> None:
    """Seam I-2: Plans 04 and 08 export ``WEB_SettingsPage`` and ``WT_TemplatesPage`` and the Platform view does the rest."""
    assert js(nav_ctx, "NV_PLAT_CUSTOM_PAGES") == {"web": "WEB_SettingsPage", "templates": "WT_TemplatesPage"}
    assert js(nav_ctx, "NV_PLAT_CUSTOM_LABELS") == {"web": "Web", "templates": "Workspace templates"}
    for name in ("NV_PLAT_CUSTOM_PAGES", "NV_PLAT_CUSTOM_LABELS", "NV_platNavAvailable", "NV_platResolveNav", "NV_platNavLabel"):
        assert nav_ctx.eval(f"typeof window.{name} !== 'undefined'"), name


@pytest.mark.parametrize(
    "nav,loaded,available",
    [
        ("providers", False, True), ("agents", False, True), ("approvals", False, True),
        ("web", False, False), ("web", True, True), ("templates", False, False), ("templates", True, True),
        ("workspaces", False, False), ("workspaces", True, False), ("bogus", False, False), ("constructor", False, False), ("toString", False, False),
    ],
)
def test_a_nav_is_available_when_it_has_a_page_or_a_loaded_component(nav_ctx, nav: str, loaded: bool, available: bool) -> None:
    if loaded:
        nav_ctx.eval(LOAD_BOTH)
    assert nav_ctx.eval(f"NV_platNavAvailable({json.dumps(nav)})") is available
    assert nav_ctx.eval(f"NV_platResolveNav({json.dumps(nav)})") == (nav if available else "providers")


@pytest.mark.parametrize(
    "nav,label",
    [("providers", "Providers"), ("profiles", "LLM Profiles"), ("channels", "Channels"), ("agents", "Agents"), ("approvals", "Approval policies"),
     ("web", "Web"), ("templates", "Workspace templates"), ("not-a-nav", "not-a-nav")],
)
def test_a_nav_label_is_providers_the_custom_label_or_the_pages_title(nav_ctx, nav: str, label: str) -> None:
    assert nav_ctx.eval(f"NV_platNavLabel({json.dumps(nav)})") == label


def test_the_current_nav_resolves_through_availability(nav_ctx) -> None:
    assert nav_ctx.eval('NV_platNavCurrent({ name: "platform", nav: "web", sub: null })') == "providers", "a custom page that is not loaded is Providers"
    nav_ctx.eval(LOAD_BOTH)
    assert nav_ctx.eval('NV_platNavCurrent({ name: "platform", nav: "web", sub: null })') == "web"
    assert nav_ctx.eval('NV_platNavCurrent({ name: "platform", nav: "templates", sub: null })') == "templates"
