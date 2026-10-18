"""The platform kit's page primitives: header, card grid, card and the paged filter (Platform revamp Plan 01, Task 1).

``ui/components/shared/platform-kit.jsx`` holds the master plan's kit contract. Every revamped Platform page draws the same header (title on the left;
filter, pager and one compact action on the right, design 5.1), the same full-width card grid (design 5.3) and the same card (style C, design 5.2),
and pages its rows the same way. Two settled additions ride on the card: its ``menu`` is a "⋯" button that opens a menu (design 20 Q1), and its
``status`` may be a button (seam I-8). The lead later made the header's ``filter`` and ``pager`` optional: a detail page draws a title and an action.

The kit runs here for real: transpiled the way the server bundles it and mounted on the hook runtime of ``tests/ui/_mini_react.py``. The prelude
records every ``useEscape`` registration (``__layers``) and every ``document`` listener (``DOC``), so the menu's Escape layer and its outside
click are driven, not grepped. The CSS rules are read from ``ui/styles.css`` with a small rule parser (comments stripped).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "ui" / "components" / "shared" / "platform-kit.jsx"
INDEX = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

PRELUDE = r"""
var __layers = [];
window.primerApi = { useEscape: function (h, a) { __layers.push({ handler: h, active: a === undefined ? true : !!a }); } };
var DOC = {};
var document = { addEventListener: function (t, f) { DOC[t] = f; }, removeEventListener: function (t, f) { if (DOC[t] === f) delete DOC[t]; } };
var CALLS = [];
function rec(n) { return function () { CALLS.push([n].concat([].slice.call(arguments))); }; }
var PF = null;
function PagedHost(p) { PF = PK_usePagedFilter(p.items, { pageSize: p.pageSize, text: function (x) { return x.name; } }); return null; }
"""

HEADER = ('{ title: "Agents", count: "13 agents",'
          ' filter: { value: "", onChange: rec("filter"), label: "Filter agents", testid: "nv-plat-filter" },'
          ' pager: { page: 0, pages: 2, from: 1, to: 12, total: 13, onPage: rec("page") },'
          ' action: { label: "+ Agent", onClick: rec("create"), testid: "nv-plat-create" } }')

CARD = ('{ id: "p1", icon: { path: "M0 0h4" }, title: "llm-openchat", subtitle: "OpenAI completions",'
        ' facts: ["vLLM · 1 model"], onOpen: rec("open") }')


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def names(ctx) -> list[str]:
    """The names of the recorded callbacks, in order (a callback handed an event records it too; the name is what a test reads)."""
    return [c[0] for c in js(ctx, "CALLS")]


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(KIT)


@pytest.fixture
def kit(code):
    ctx = mini_react_context(code, PRELUDE)
    try:
        yield ctx
    finally:
        ctx.close()


# ---- a small CSS rule reader (shared with the form and card tests of the kit) -----------------------------------------------------------------------


def css_rules(css: str = CSS) -> list[tuple[str | None, list[str], str]]:
    """Every rule of ``css`` as ``(enclosing at-rule or None, [selectors], body)``, comments stripped; nested rules (an ``@media`` block) keep their at-rule."""
    text = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: list[tuple[str | None, list[str], str]] = []
    stack: list[tuple[str, int]] = []
    head = 0
    for i, c in enumerate(text):
        if c == "{":
            stack.append((text[head:i].strip(), i + 1))
            head = i + 1
        elif c == "}":
            prelude, start = stack.pop()
            if not prelude.startswith("@"):
                out.append((stack[-1][0] if stack else None, [s.strip() for s in prelude.split(",")], text[start:i]))
            head = i + 1
    assert not stack, "the stylesheet's braces do not balance"
    return out


def css_body(selector: str, media: str | None = None, css: str = CSS) -> str:
    """The bodies of every rule (under ``media``, or top level) whose selector list holds ``selector`` exactly, joined."""
    bodies = [body for at, selectors, body in css_rules(css) if at == media and selector in selectors]
    assert bodies, f"no rule for {selector!r} under {media!r}"
    return "\n".join(bodies)


# the transform property itself, not text-transform
TRANSFORM = re.compile(r"(?<![\w-])transform\s*:")


def declarations(body: str) -> dict[str, str]:
    out = {}
    for part in body.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = " ".join(v.split())
    return out


# ---- the header -------------------------------------------------------------------------------------------------------------------------------------


def test_the_header_draws_filter_then_pager_then_action(kit) -> None:
    kit.eval(f"MR.mount(PK_PageHeader, {HEADER})")
    ids = [e.get("testid") for e in js(kit, 'MR.subtree("pk-header")') if e.get("testid")]
    assert ids.index("nv-plat-filter") < ids.index("pk-pager") < ids.index("pk-pager-next") < ids.index("nv-plat-create")
    assert {"Agents", "13 agents", "1-12 of 13", "+ Agent"} <= set(js(kit, "MR.texts()"))
    assert kit.eval('MR.find("pk-header").props.children[0].type') == "h2", "the page title is the page's heading (the view's h1 is the shell's)"


def test_the_filter_is_named_by_its_label_and_reports_typing(kit) -> None:
    kit.eval(f"MR.mount(PK_PageHeader, {HEADER}); MR.find('nv-plat-filter').props.onChange({{ target: {{ value: 'ab' }} }});")
    assert kit.eval("MR.find('nv-plat-filter').props['aria-label']") == "Filter agents"
    assert kit.eval("MR.find('nv-plat-filter').props.placeholder") == "Filter…"
    assert js(kit, "CALLS") == [["filter", "ab"]]


def test_the_filter_testid_defaults_to_pk_filter(kit) -> None:
    kit.eval('MR.mount(PK_PageHeader, { title: "Graphs", filter: { value: "x", onChange: rec("filter"), label: "Filter graphs" },'
             ' pager: { page: 0, pages: 1, from: 1, to: 1, total: 1, onPage: rec("page") } })')
    assert kit.eval('MR.find("pk-filter").type') == "input"
    assert kit.eval('MR.find("pk-filter").props.value') == "x"


def test_the_pager_disables_previous_on_the_first_page_and_next_on_the_last(kit) -> None:
    kit.eval(f"MR.mount(PK_PageHeader, {HEADER})")
    assert kit.eval('MR.find("pk-pager-prev").props.disabled') is True
    assert kit.eval('MR.find("pk-pager-next").props.disabled') is False
    assert kit.eval('MR.find("pk-pager-prev").props["aria-label"]') == "Previous page"
    assert kit.eval('MR.find("pk-pager-next").props["aria-label"]') == "Next page"
    kit.eval('MR.click("pk-pager-next")')
    assert js(kit, "CALLS") == [["page", 1]]
    kit.eval("MR.rerender(Object.assign(" + HEADER + ', { pager: { page: 1, pages: 2, from: 13, to: 13, total: 13, onPage: rec("page") } }))')
    assert kit.eval('MR.find("pk-pager-next").props.disabled') is True
    assert kit.eval('MR.find("pk-pager-prev").props.disabled') is False
    kit.eval('MR.click("pk-pager-prev")')
    assert js(kit, "CALLS")[-1] == ["page", 0]


def test_an_empty_list_reads_0_of_0(kit) -> None:
    kit.eval('MR.mount(PK_PageHeader, { title: "Agents", filter: { value: "", onChange: rec("filter"), label: "Filter agents" },'
             ' pager: { page: 0, pages: 1, from: 0, to: 0, total: 0, onPage: rec("page") } })')
    assert kit.eval('MR.find("pk-pager-range").props.children') == "0 of 0"
    assert kit.eval('MR.find("pk-pager-prev").props.disabled') is True and kit.eval('MR.find("pk-pager-next").props.disabled') is True


def test_extra_sits_left_of_the_filter_and_no_action_draws_no_button(kit) -> None:
    kit.eval('MR.mount(PK_PageHeader, { title: "Channels", extra: React.createElement("button", { "data-testid": "x-extra", className: "pk-extra" }, "Rules"),'
             ' filter: { value: "", onChange: rec("filter"), label: "Filter channels", testid: "nv-plat-filter" },'
             ' pager: { page: 0, pages: 1, from: 0, to: 0, total: 0, onPage: rec("page") } })')
    tree = js(kit, 'MR.subtree("pk-header")')
    ids = [e.get("testid") for e in tree if e.get("testid")]
    assert ids.index("x-extra") < ids.index("nv-plat-filter")
    assert not [e for e in tree if "pk-action" in e["className"].split()], "no action, no button"


def test_the_action_label_is_drawn_verbatim(kit) -> None:
    kit.eval(f"MR.mount(PK_PageHeader, {HEADER})")
    assert kit.eval('MR.find("nv-plat-create").props.children') == "+ Agent"
    assert kit.eval('MR.find("nv-plat-create").props.type') == "button"
    kit.eval('MR.click("nv-plat-create")')
    assert names(kit) == ["create"]
    kit.eval('MR.mount(PK_PageHeader, { title: "Tools", action: { label: "Refresh", onClick: rec("refresh"), testid: "x-refresh" } })')
    assert kit.eval('MR.find("x-refresh").props.children') == "Refresh", "the header adds no '+' of its own"


def test_a_header_without_filter_or_pager_draws_neither(kit) -> None:
    """The lead's addition for Plan 02: a detail page's header is a title and an action, so the filter and the pager are optional and an omitted one is not drawn."""
    kit.eval('MR.mount(PK_PageHeader, { title: "llm-openchat", action: { label: "Edit", onClick: rec("edit"), testid: "x-edit" } })')
    tree = js(kit, 'MR.subtree("pk-header")')
    assert [e.get("testid") for e in tree if e.get("testid")] == ["pk-header", "x-edit"]
    assert not [e for e in tree if e["type"] == "input"], "no filter box"
    assert not [e for e in tree if "pk-filter" in e["className"].split() or "pk-pager" in e["className"].split()]
    kit.eval('MR.mount(PK_PageHeader, { title: "Decisions", filter: { value: "", onChange: rec("filter"), label: "Filter decisions" } })')
    assert kit.eval('!!MR.find("pk-filter") && !MR.find("pk-pager")'), "a filter without a pager"
    kit.eval('MR.mount(PK_PageHeader, { title: "Decisions", pager: { page: 0, pages: 1, from: 1, to: 3, total: 3, onPage: rec("page") } })')
    assert kit.eval('!MR.find("pk-filter") && !!MR.find("pk-pager")'), "a pager without a filter"
    assert kit.eval('MR.find("pk-pager-range").props.children') == "1-3 of 3"


def test_the_header_controls_share_one_height_and_the_action_is_filled() -> None:
    for selector in (".pk-filter", ".pk-pager-btn", ".pk-action", ".pk-extra"):
        assert declarations(css_body(selector)).get("height") == "calc(var(--hit) - 4px)", selector
    action = declarations(css_body(".pk-action"))
    assert action["background"] == "var(--accent)" and action["color"] == "var(--accent-fg)"
    assert declarations(css_body(".pk-filter"))["width"] == "200px"


def test_on_a_phone_the_header_controls_are_tap_sized_and_the_filter_takes_a_row() -> None:
    phone = "@media (max-width: 639px)"
    for selector in (".pk-action", ".pk-extra", ".pk-pager-btn", ".pk-card-open", ".pk-card-menu-btn", ".pk-card-menu-item"):
        assert declarations(css_body(selector, phone)).get("min-height") == "var(--tap-min)", selector
    assert declarations(css_body(".pk-filter", phone)).get("flex") == "1 1 100%"


# ---- the paged filter -------------------------------------------------------------------------------------------------------------------------------


def _paged(kit, n: int = 30, size: int = 12) -> None:
    kit.eval(f"var ITEMS = []; for (var i = 0; i < {n}; i++) ITEMS.push({{ name: 'item-' + i }});"
             f"MR.mount(PagedHost, {{ items: ITEMS, pageSize: {size} }});")


def test_paged_filter_pages_by_page_size(kit) -> None:
    _paged(kit)
    assert js(kit, "[PF.pages, PF.page, PF.from, PF.to, PF.total, PF.visible.length]") == [3, 0, 1, 12, 30, 12]
    assert js(kit, "PF.visible.map(function (x) { return x.name; })")[:2] == ["item-0", "item-1"]
    kit.eval("PF.setPage(2); MR.rerender();")
    assert js(kit, "[PF.page, PF.visible.length, PF.from, PF.to]") == [2, 6, 25, 30]


def test_paged_filter_clamps_when_the_list_shrinks(kit) -> None:          # Review Focus 4
    kit.eval("var ITEMS = []; for (var i = 0; i < 30; i++) ITEMS.push({ name: 'item-' + i });"
             "MR.mount(PagedHost, { items: ITEMS, pageSize: 12 }); PF.setPage(2); MR.rerender();"
             "MR.rerender({ items: ITEMS.slice(0, 13), pageSize: 12 });")
    assert js(kit, "[PF.page, PF.visible.length, PF.from, PF.to, PF.total]") == [1, 1, 13, 13, 13]
    kit.eval("MR.rerender({ items: [], pageSize: 12 });")
    assert js(kit, "[PF.page, PF.pages, PF.visible.length, PF.from, PF.to, PF.total]") == [0, 1, 0, 0, 0, 0], "never a '25-24 of 13'"


def test_paged_filter_matches_case_insensitively_and_resets_to_page_zero(kit) -> None:
    _paged(kit)
    kit.eval("PF.setPage(2); MR.rerender();")
    assert kit.eval("PF.page") == 2
    kit.eval('PF.setQ("ITEM-1"); MR.rerender();')
    shown = js(kit, "PF.visible.map(function (x) { return x.name; })")
    assert kit.eval("PF.page") == 0, "a filter typed on page 3 sends the page back to 1"
    assert kit.eval("PF.q") == "ITEM-1"
    assert shown and all("item-1" in s for s in shown) and kit.eval("PF.total") == 11
    kit.eval('PF.setQ("  item-29  "); MR.rerender();')
    assert js(kit, "PF.visible.map(function (x) { return x.name; })") == ["item-29"], "the query is trimmed"


def test_paged_filter_set_page_clamps_to_the_range(kit) -> None:
    _paged(kit)
    kit.eval("PF.setPage(-1); MR.rerender();")
    assert kit.eval("PF.page") == 0
    kit.eval("PF.setPage(99); MR.rerender();")
    assert kit.eval("PF.page") == kit.eval("PF.pages - 1") == 2


# ---- the card ---------------------------------------------------------------------------------------------------------------------------------------


def test_a_card_draws_title_subtitle_badge_status_and_at_most_three_facts(kit) -> None:
    kit.eval('MR.mount(PK_Card, { id: "p1", icon: { path: "M0 0h4", tone: "var(--violet)" }, title: "llm-openchat", subtitle: "OpenAI completions",'
             ' badge: "built in", status: { label: "reachable", tone: "ok" },'
             ' facts: [null, "vLLM · 1 model", ["context", "262144"], "a", "b"], onOpen: rec("open") })')
    texts = js(kit, "MR.texts()")
    assert {"llm-openchat", "OpenAI completions", "built in", "reachable", "vLLM · 1 model", "context", "262144", "a"} <= set(texts)
    assert "b" not in texts, "at most three facts are drawn"
    assert kit.eval('MR.find("pk-card-status:p1").type') == "span"
    assert kit.eval('MR.find("pk-card-status:p1").props["data-tone"]') == "ok"
    assert kit.eval('MR.find("pk-card-badge:p1").props.children') == "built in"
    tree = js(kit, 'MR.subtree("pk-card:p1")')
    classes = [e["className"] for e in tree]
    assert classes.index("pk-card-title") < classes.index("pk-card-sub") < classes.index("pk-card-facts") < classes.index("pk-card-foot")
    assert [c for c in classes if c.startswith("pk-fact")] == ["pk-fact pk-fact-line", "pk-fact", "pk-fact-k", "pk-fact-v", "pk-fact pk-fact-line"]


def test_a_card_icon_is_tinted_by_its_tone(kit) -> None:
    kit.eval('MR.mount(PK_Card, { id: "g", icon: { path: "M1 1h2", tone: "var(--violet)" }, title: "g", onOpen: rec("open") })')
    icon = [e for e in js(kit, 'MR.subtree("pk-card:g")') if "pk-card-icon" in e["className"].split()]
    assert len(icon) == 1
    kit.eval('var TILE = null; (function walk(n) { if (!n || typeof n !== "object") return; if (Array.isArray(n)) { n.forEach(walk); return; }'
             ' if (n.props && n.props.className === "pk-card-icon") TILE = n; walk(n.children); walk(n.out); })(MR.find("pk-card:g"));')
    assert js(kit, "TILE.props.style") == {"color": "var(--violet)"}
    assert kit.eval('TILE.props["aria-hidden"]') == "true"


def test_card_open_is_a_named_button_and_the_card_has_no_click(kit) -> None:
    kit.eval(f"MR.mount(PK_Card, {CARD})")
    assert kit.eval('MR.find("pk-card-open:p1").type') == "button"
    assert kit.eval('MR.find("pk-card-open:p1").props.type') == "button"
    assert kit.eval('MR.find("pk-card-open:p1").props["aria-label"]') == "Open llm-openchat"
    assert kit.eval('MR.find("pk-card-open:p1").props.children') == "Open"
    assert kit.eval('MR.find("pk-card:p1").props.onClick === undefined'), "the card is opened by its stretched Open button, not by a click on the div"
    kit.eval('MR.click("pk-card-open:p1")')
    assert names(kit) == ["open"]


def test_an_actionable_status_is_a_button_named_by_its_label_and_never_opens_the_card(kit) -> None:      # seam I-8
    kit.eval('MR.mount(PK_Card, { id: "p1", icon: { path: "M0 0h4" }, title: "search", onOpen: rec("open"),'
             ' status: { label: "Internal search: off", tone: "warn", onClick: rec("status"), testid: "ts-internal-search" } })')
    assert kit.eval('MR.find("ts-internal-search").type') == "button"
    assert kit.eval('MR.find("ts-internal-search").props.type') == "button"
    assert kit.eval('MR.find("ts-internal-search").props.children') == "Internal search: off", "named by its label"
    assert kit.eval('MR.find("ts-internal-search").props.className').split() == ["pk-card-status", "pk-card-status-btn"]
    assert kit.eval('MR.find("ts-internal-search").props["data-tone"]') == "warn"
    kit.eval('MR.click("ts-internal-search")')
    assert names(kit) == ["status"], "the status ran its action once and did not open the card"
    kit.eval('MR.mount(PK_Card, { id: "p2", icon: { path: "M0 0h4" }, title: "web", onOpen: rec("open"), status: { label: "available", tone: "ok" } })')
    assert kit.eval('MR.find("pk-card-status:p2").type') == "span", "a status without an action is not a control"


def test_the_menu_button_opens_the_items_and_a_pick_runs_once_and_closes(kit) -> None:
    kit.eval('MR.mount(PK_Card, { id: "p1", icon: { path: "M0 0h4" }, title: "llm-openchat", onOpen: rec("open"),'
             ' menu: [{ label: "Delete", danger: true, onClick: rec("delete"), testid: "nv-plat-delete:p1" }] })')
    btn = "MR.find('pk-card-menu:p1')"
    assert kit.eval(f"{btn}.props.children") == "⋯"
    assert kit.eval(f"{btn}.props.type") == "button"
    assert kit.eval(f"{btn}.props['aria-label']") == "More actions for llm-openchat"
    assert kit.eval(f"{btn}.props['aria-haspopup']") == "menu"
    assert kit.eval(f"{btn}.props['aria-expanded']") == "false"
    assert kit.eval("MR.find('pk-card-menu-list:p1') === null"), "closed until asked"
    kit.eval("MR.click('pk-card-menu:p1')")
    assert kit.eval(f"{btn}.props['aria-expanded']") == "true"
    assert kit.eval("MR.find('pk-card-menu-list:p1').props.role") == "menu"
    item = "MR.find('nv-plat-delete:p1')"
    assert kit.eval(f"{item}.props.role") == "menuitem"
    assert kit.eval(f"{item}.props['data-danger']") == "true"
    assert kit.eval(f"{item}.props.children") == "Delete"
    kit.eval("MR.click('nv-plat-delete:p1')")
    assert names(kit) == ["delete"], "the pick ran once and never opened the card"
    assert kit.eval("MR.find('pk-card-menu-list:p1') === null"), "a pick closes the menu"


def test_the_trigger_and_the_list_keep_their_clicks_to_themselves(kit) -> None:
    """Review Focus 3: the menu sits over the stretched Open, so its clicks must stop where they land."""
    kit.eval('MR.mount(PK_Card, { id: "p1", icon: { path: "M0 0h4" }, title: "t", onOpen: rec("open"),'
             ' menu: [{ label: "Delete", danger: true, onClick: rec("delete"), testid: "d:p1" }] })')
    kit.eval("var STOPPED = 0; function ev() { return { preventDefault: function () {}, stopPropagation: function () { STOPPED++; } }; }"
             "MR.find('pk-card-menu:p1').props.onClick(ev()); MR.rerender();"
             "MR.find('pk-card-menu-list:p1').props.onClick(ev()); MR.rerender();")
    assert kit.eval("STOPPED") == 2
    assert kit.eval("!!MR.find('pk-card-menu-list:p1')"), "a click inside the list does not close it"
    kit.eval("MR.find('d:p1').props.onClick(ev()); MR.rerender();")
    assert kit.eval("STOPPED") == 3 and names(kit) == ["delete"]


def test_the_menu_is_an_escape_layer_only_while_open(kit) -> None:      # Review Focus 3
    kit.eval('MR.mount(PK_Card, { id: "p1", icon: { path: "M0 0h4" }, title: "t", onOpen: rec("open"),'
             ' menu: [{ label: "Delete", onClick: rec("delete"), testid: "d:p1" }] })')
    assert js(kit, "__layers[__layers.length - 1].active") is False, "a closed menu does not answer Escape"
    kit.eval("MR.click('pk-card-menu:p1')")
    assert js(kit, "__layers[__layers.length - 1].active") is True
    kit.eval("var PREVENTED = 0; __layers[__layers.length - 1].handler({ preventDefault: function () { PREVENTED++; } }); MR.rerender();")
    assert kit.eval("MR.find('pk-card-menu-list:p1') === null"), "Escape closes the menu"
    assert kit.eval("PREVENTED") == 1, "and says it handled the key"
    assert js(kit, "__layers[__layers.length - 1].active") is False
    assert names(kit) == [], "Escape picks nothing"


def test_the_menu_closes_on_an_outside_click(kit) -> None:
    kit.eval('MR.mount(PK_Card, { id: "p1", icon: { path: "M0 0h4" }, title: "t", onOpen: rec("open"),'
             ' menu: [{ label: "Delete", onClick: rec("delete"), testid: "d:p1" }] })')
    assert kit.eval("typeof DOC.click") == "undefined", "no document listener while closed"
    kit.eval("MR.click('pk-card-menu:p1')")
    assert kit.eval("typeof DOC.click") == "function"
    kit.eval("DOC.click({}); MR.rerender();")
    assert kit.eval("MR.find('pk-card-menu-list:p1') === null")
    assert kit.eval("typeof DOC.click") == "undefined", "the listener goes with the menu"
    assert names(kit) == []


def test_a_card_with_no_menu_draws_no_menu_button(kit) -> None:
    kit.eval(f"MR.mount(PK_Card, {CARD})")
    assert kit.eval("MR.find('pk-card-menu:p1') === null")
    kit.eval('MR.mount(PK_Card, { id: "p1", icon: { path: "M0 0h4" }, title: "t", onOpen: rec("open"), menu: [] })')
    assert kit.eval("MR.find('pk-card-menu:p1') === null"), "an empty menu is no menu"
    assert js(kit, "__layers.filter(function (l) { return l.active; })") == []


def test_card_css_stretches_open_and_lifts_menu_and_status_above_it() -> None:
    assert declarations(css_body(".pk-card")).get("position") == "relative"
    after = declarations(css_body(".pk-card-open::after"))
    assert after.get("content") == '""' and after.get("position") == "absolute" and after.get("inset") == "0"
    assert "pointer-events" not in after and "pointer-events" not in declarations(css_body(".pk-card-open"))
    assert "position" not in declarations(css_body(".pk-card-open")), "a positioned Open would stretch over itself, not the card"
    for selector in (".pk-card-menu-btn", ".pk-card-menu-list", ".pk-card-status-btn"):
        d = declarations(css_body(selector))
        assert d.get("position") in ("relative", "absolute"), selector
        assert int(d.get("z-index", "0")) >= 1, f"{selector} must sit above the stretched Open"
    moved = [s for _at, selectors, body in css_rules() for s in selectors if s.startswith(".pk-") and TRANSFORM.search(body)]
    assert not moved, f"a kit rule that transforms makes a containing block of its own: {moved}"


def test_the_grid_is_full_width_auto_fill_240() -> None:
    grid = declarations(css_body(".pk-grid"))
    assert grid["grid-template-columns"] == "repeat(auto-fill, minmax(240px, 1fr))"
    assert grid.get("align-items") == "stretch", "cards in a row share one height"
    assert "max-width" not in grid
    card = declarations(css_body(".pk-card"))
    assert card["border-radius"] == "var(--r-12)" and card["box-shadow"] == "var(--pk-card-shadow)"
    assert declarations(css_body(".pk-card-foot")).get("margin-top") == "auto", "Open lines up across a row"
    dark = declarations(css_body(':root[data-theme="dark"]'))
    light = declarations(css_body(':root[data-theme="light"]'))
    assert dark.get("--pk-card-shadow") and light.get("--pk-card-shadow")
    assert dark["--pk-card-shadow"] != light["--pk-card-shadow"]


# ---- load order and exports -------------------------------------------------------------------------------------------------------------------------

# Loaded in the shared block ahead of form-field.jsx and drawing nothing from the kit: a pure session-state decoder (the list may only shrink).
LOADED_BEFORE_THE_KIT = {"components/session-state.jsx"}


def _babel_order() -> list[str]:
    return re.findall(r'<script[^>]*type="text/babel"[^>]*src="([^"]+)"', INDEX)


def test_the_kit_loads_after_shared_and_form_field_and_before_every_page_component() -> None:
    order = _babel_order()
    at = order.index("components/shared/platform-kit.jsx")
    assert order.index("components/shared.jsx") < at and order.index("components/shared/form-field.jsx") < at
    assert order[at - 1] == "components/shared/form-field.jsx", "right after the FormField it wraps"
    pages = [src for src in order if src.startswith("components/") and not src.startswith("components/shared/")
             and src != "components/shared.jsx" and src not in LOADED_BEFORE_THE_KIT]
    assert pages and all(order.index(src) > at for src in pages), [src for src in pages if order.index(src) < at]
    for src in LOADED_BEFORE_THE_KIT:
        assert order.index(src) < at and "PK_" not in (ROOT / "ui" / src).read_text(encoding="utf-8")


def test_the_page_primitives_are_on_window(kit) -> None:
    for name in ("PK_PageHeader", "PK_CardGrid", "PK_Card", "PK_usePagedFilter"):
        assert kit.eval(f"typeof window.{name}") == "function", name


def test_the_card_grid_holds_its_children(kit) -> None:
    kit.eval('MR.mount(PK_CardGrid, { testid: "g", children: [React.createElement(PK_Card, Object.assign({ key: "a" }, ' + CARD + ')),'
             ' React.createElement(PK_Card, Object.assign({ key: "b" }, ' + CARD.replace('"p1"', '"p2"') + '))] })')
    assert kit.eval('MR.find("g").props.className') == "pk-grid"
    assert kit.eval('MR.findAll("pk-card:").length') == 2
