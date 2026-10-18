"""The Platform sidebar and the page it opens (Platform revamp Plan 01, Tasks 6 and 7; seams I-1, I-2 and I-7).

The real ``NV_PlatNav`` and ``NV_PlatPage`` run here on the page harness (``tests/ui/_platform_page_harness.py``), with the real grammar of ``ui/foundation/shell-url.js`` loaded
beside them (the sidebar reads its type list from ``window.SH_VIEW_SUBS``, so a stand-in list would test the stand-in) and a recording ``window.ProviderCatalog`` where the real one
would mount.

Task 6 (this file's first half): the Providers row has one sub-entry per provider type, always visible; the entry of the open type is the current one and the Providers row is current
only on All; a click goes to the type; the page mounts the catalog at the type.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._platform_page_harness import platform_page_context

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


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def nav_ctx():
    ctx = platform_page_context(prelude_extra=SHELL_URL + CATALOG)
    try:
        yield ctx
    finally:
        ctx.close()


def _sub_ids(ctx) -> list[str]:
    return js(ctx, 'MR.findAll("nv-plat-sub:").map(function (e) { return e.props["data-testid"].slice("nv-plat-sub:".length); })')


# ---- Task 6: the Providers sub-entries ------------------------------------------------------------------------------------------------


def test_the_providers_row_has_one_sub_entry_per_type_in_order(nav_ctx) -> None:
    nav_ctx.eval("MR.mount(NV_PlatNav, {});")
    assert _sub_ids(nav_ctx) == TYPES
    assert js(nav_ctx, 'MR.findAll("nv-plat-sub:").map(function (e) { return e.props.children; })') == LABELS


def test_the_sub_entries_sit_in_one_labelled_group_under_the_providers_row(nav_ctx) -> None:
    nav_ctx.eval("MR.mount(NV_PlatNav, {});")
    assert js(nav_ctx, 'MR.find("nv-plat-subs").props.role') == "group"
    assert js(nav_ctx, 'MR.find("nv-plat-subs").props["aria-label"]') == "Provider types"
    assert js(nav_ctx, 'MR.find("nv-plat-subs").props.className') == "nv-plat-subs"
    order = js(nav_ctx, 'MR.subtree("nv-plat-nav").map(function (e) { return e.testid; }).filter(Boolean)')
    providers = order.index("nv-plat-row:providers")
    assert order[providers + 1] == "nv-plat-subs"
    assert order[providers + 2:providers + 2 + len(TYPES)] == ["nv-plat-sub:" + t for t in TYPES], "the types follow the Providers row, before the next row"
    assert order[providers + 2 + len(TYPES)] == "nv-plat-row:profiles"


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
