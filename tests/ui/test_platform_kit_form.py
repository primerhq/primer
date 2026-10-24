"""The platform kit's form primitives and the Modal's two new props (Platform revamp Plan 01, Task 2).

The overlays the revamp rebuilds (Register provider, the profile overlays, agent, template, new workspace) share one shell and one set of form
parts: ``PK_FormOverlay`` (a ``Modal`` with the kit class, 720px wide, a centred title, the type selector centre-top and a footer with Cancel left
and the action right, design 6.1 and 6.2), ``PK_Grid``, ``PK_Field`` (a ``FormField`` whose help and error sit UNDER the control and which never
draws the inline ``hint``), ``PK_Section``, ``PK_Fold``, ``PK_Segmented``, ``PK_RowTable`` and ``PK_CheckTable``. The lead added one thing for
Plan 02: a ``PK_CheckTable`` row may be ``disabled`` (it cannot be toggled, toggle-all leaves it out, it is drawn muted).

The context holds the REAL ``shared.jsx`` (``Modal``, ``Icon``), ``shared/form-field.jsx`` and the kit, transpiled the way the server bundles them,
on the hook runtime of ``tests/ui/_mini_react.py``. ``ALL(testid)`` walks every element at or under the one with that test id (the harness's own
walk), so an element without a test id (a label, the dialog) can be read too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile
from tests.ui.test_platform_kit_page import TRANSFORM, css_body, css_rules, declarations

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "components"
SHARED_SRC = (UI / "shared.jsx").read_text(encoding="utf-8")

PRELUDE = r"""
var __layers = [];
var MOBILE = false;
window.primerApi = {
  useEscape: function (h, a) { __layers.push({ handler: h, active: a === undefined ? true : !!a }); },
  useFocusTrap: function () {}, useViewport: function () { return { isMobile: MOBILE }; },
};
var document = { addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
var CALLS = [];
function rec(n) { return function () { CALLS.push([n].concat([].slice.call(arguments))); }; }
function el(type, props) { return React.createElement.apply(null, [type, props || null].concat([].slice.call(arguments, 2))); }
function host(child) { return function Host() { return el("div", { "data-testid": "host" }, child); }; }
function ALL(testid) {
  var out = [];
  (function walk(n) {
    if (n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (!n.__el) return;
    out.push(n);
    if (typeof n.type === "function" && n.type !== React.Fragment) walk(n.out); else walk(n.children);
  })(MR.find(testid));
  return out;
}
function textOf(node) {
  var out = [];
  (function collect(n) {
    if (n == null || typeof n === "boolean") return;
    if (typeof n === "string" || typeof n === "number") { out.push(String(n)); return; }
    if (Array.isArray(n)) { n.forEach(collect); return; }
    if (n.__el) collect(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  })(node);
  return out.join("");
}
"""

SELECTOR = ('{ prefix: "Register a", value: "llm", testid: "sel", onChange: rec("sel"),'
            ' options: [{ value: "llm", label: "LLM provider", iconPath: "M0 0h4" }, { value: "embedding", label: "Embedding provider" }] }')


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture(scope="module")
def code() -> str:
    return "\n".join(transpile(p) for p in (UI / "shared.jsx", UI / "shared" / "form-field.jsx", UI / "shared" / "platform-kit.jsx"))


@pytest.fixture
def form(code):
    ctx = mini_react_context(code, PRELUDE)
    try:
        yield ctx
    finally:
        ctx.close()


# ---- the overlay ------------------------------------------------------------------------------------------------------------------------------------


def test_the_overlay_is_a_kit_modal_720_wide_with_its_testid(form) -> None:
    form.eval('MR.mount(PK_FormOverlay, { title: "Register provider", onClose: rec("close"), testid: "pc-register",'
              ' footer: { left: React.createElement("button", { "data-testid": "f-cancel" }, "Cancel"),'
              ' right: React.createElement("button", { "data-testid": "f-next" }, "Next") } })')
    assert form.eval('MR.find("pc-register").props.className').split() == ["modal", "pk-overlay"]
    assert form.eval('MR.find("pc-register").props.role') == "dialog"
    assert js(form, 'MR.find("pc-register").props.style') == {"width": 720}
    ids = [e.get("testid") for e in js(form, 'MR.subtree("pc-register")') if e.get("testid") in ("f-cancel", "f-next")]
    assert ids == ["f-cancel", "f-next"]
    assert "Register provider" in js(form, "MR.texts()")
    form.eval('MR.rerender({ title: "Wide", onClose: rec("close"), testid: "pc-register", width: 900, footer: { right: null } })')
    assert js(form, 'MR.find("pc-register").props.style') == {"width": 900}


def test_the_footer_puts_left_in_pk_foot_left_and_right_in_pk_foot_right(form) -> None:
    form.eval('MR.mount(PK_FormOverlay, { title: "T", onClose: rec("close"), testid: "ov",'
              ' footer: { left: el("button", { "data-testid": "f-cancel" }, "Cancel"), right: el("button", { "data-testid": "f-save" }, "Save") } })')
    tree = js(form, 'MR.subtree("ov")')
    classes = [e["className"] for e in tree]
    ids = [e.get("testid") for e in tree]
    foot, left, right = classes.index("pk-foot"), classes.index("pk-foot-left"), classes.index("pk-foot-right")
    assert foot < left < ids.index("f-cancel") < right < ids.index("f-save")
    assert classes[foot - 1] == "modal-f", "the kit footer fills the Modal's footer bar"


def test_the_selector_shows_prefix_and_option_and_switches(form) -> None:
    form.eval(f'MR.mount(PK_FormOverlay, {{ title: "Register provider", onClose: rec("close"), testid: "ov", selector: {SELECTOR}, footer: {{ right: null }} }})')
    assert "Register a" in js(form, "MR.texts()")
    assert "LLM provider" in form.eval('textOf(MR.find("sel"))')
    assert form.eval('MR.find("sel").props["aria-haspopup"]') == "menu"
    assert form.eval('MR.find("sel").props["aria-expanded"]') == "false"
    assert form.eval('MR.find("sel:menu") === null')
    form.eval('MR.click("sel")')
    assert form.eval('MR.find("sel:menu").props.role') == "menu"
    assert form.eval('MR.find("sel:llm").props.role') == "menuitemradio"
    assert form.eval('MR.find("sel:llm").props["aria-checked"]') == "true"
    assert form.eval('MR.find("sel:embedding").props["aria-checked"]') == "false"
    form.eval('MR.click("sel:embedding")')
    assert js(form, "CALLS") == [["sel", "embedding"]]
    assert form.eval('MR.find("sel:menu") === null'), "a pick closes the menu"
    classes = [e["className"] for e in js(form, 'MR.subtree("ov")') if e["type"] != "component"]
    body = classes.index("modal-b")
    assert classes.index("pk-selector") == body + 1, "the selector is the first thing in the body, under the centred title"


def test_a_locked_selector_shows_its_value_and_has_no_menu(form) -> None:
    locked = SELECTOR.replace('testid: "sel"', 'testid: "sel", locked: true')
    form.eval(f'MR.mount(PK_FormOverlay, {{ title: "Edit provider", onClose: rec("close"), testid: "ov", selector: {locked}, footer: {{ right: null }} }})')
    assert form.eval('MR.find("sel").props["aria-disabled"]') == "true"
    assert "LLM provider" in form.eval('textOf(MR.find("sel"))')
    assert "▾" not in form.eval('textOf(MR.find("sel"))'), "no caret on a type that cannot change"
    form.eval('MR.click("sel")')
    assert form.eval('MR.find("sel:menu") === null')
    assert js(form, "CALLS") == []


def test_the_selector_menu_is_an_escape_layer_only_while_open(form) -> None:
    form.eval(f'MR.mount(PK_FormOverlay, {{ title: "Register provider", onClose: rec("close"), testid: "ov", selector: {SELECTOR}, footer: {{ right: null }} }})')
    assert js(form, "__layers[__layers.length - 1].active") is False, "the closed menu does not answer Escape"
    assert js(form, "__layers[__layers.length - 2].active") is True, "the Modal's own layer"
    form.eval('MR.click("sel")')
    assert js(form, "[__layers[__layers.length - 2].active, __layers[__layers.length - 1].active]") == [True, True]
    form.eval("var P = 0; __layers[__layers.length - 1].handler({ preventDefault: function () { P++; } }); MR.rerender();")
    assert form.eval('MR.find("sel:menu") === null'), "Escape closes the menu"
    assert form.eval("P") == 1
    assert js(form, "CALLS") == [], "and not the overlay under it"
    assert js(form, "__layers[__layers.length - 2].active") is True, "the overlay's layer stays registered"


# ---- the additions of Plan 08 (lead ruling 1: K-1, K-3, K-4; optional and additive) ------------------------------------------------------------------

SEARCHABLE = ('{ prefix: "From template", value: "python-dev", testid: "tpl", onChange: rec("tpl"), searchable: true,'
              ' options: [{ value: "python-dev", label: "python-dev" }, { value: "node-dev", label: "Node 20" }, { value: "k-big", label: "Big box" }] }')


def _menu_items(form, testid: str) -> list:
    return js(form, f'ALL("{testid}:menu").filter(function (n) {{ return n.props.role === "menuitemradio"; }}).map(function (n) {{ return n.props["data-testid"]; }})')


def test_k3_each_selector_item_carries_a_testid_of_its_value(form) -> None:
    """K-3 asked for a test id on each item for the journeys. The kit already had one, ``<selector.testid>:<value>``, which the Register overlay's tests and
    journeys use, so the item keeps that one (an element holds one test id) and the template journeys pick ``ws-template-provider:<id>``."""
    form.eval(f'MR.mount(PK_FormOverlay, {{ title: "T", onClose: rec("close"), testid: "ov", selector: {SELECTOR}, footer: {{ right: null }} }})')
    form.eval('MR.click("sel")')
    assert _menu_items(form, "sel") == ["sel:llm", "sel:embedding"]


def test_k1_a_searchable_selector_filters_its_items_by_label_and_value(form) -> None:
    form.eval(f'MR.mount(PK_FormOverlay, {{ title: "New workspace", onClose: rec("close"), testid: "ov", selector: {SEARCHABLE}, footer: {{ right: null }} }})')
    assert form.eval('MR.find("tpl:search") === null'), "the box is in the open menu only"
    form.eval('MR.click("tpl")')
    search = js(form, 'MR.find("tpl:search").props')
    assert search["aria-label"] == "Filter the list" and search["value"] == ""
    assert _menu_items(form, "tpl") == ["tpl:python-dev", "tpl:node-dev", "tpl:k-big"]
    form.eval('MR.find("tpl:search").props.onChange({ target: { value: "NODE" } }); MR.rerender();')
    assert _menu_items(form, "tpl") == ["tpl:node-dev"], "the label matches, whatever the case"
    form.eval('MR.find("tpl:search").props.onChange({ target: { value: "k-b" } }); MR.rerender();')
    assert _menu_items(form, "tpl") == ["tpl:k-big"], "the value matches too"
    form.eval('MR.find("tpl:search").props.onChange({ target: { value: "zzz" } }); MR.rerender();')
    assert _menu_items(form, "tpl") == [] and "No match" in form.eval('textOf(MR.find("tpl:popup"))')
    form.eval('MR.find("tpl:search").props.onChange({ target: { value: "dev" } }); MR.rerender();')
    form.eval('MR.click("tpl:node-dev")')
    assert js(form, "CALLS") == [["tpl", "node-dev"]] and form.eval('MR.find("tpl:menu") === null')
    form.eval('MR.click("tpl")')
    assert js(form, 'MR.find("tpl:search").props.value') == "" and len(_menu_items(form, "tpl")) == 3, "a menu opens with no filter"


def test_k1_enter_in_the_box_picks_the_first_item_left(form) -> None:
    form.eval(f'MR.mount(PK_FormOverlay, {{ title: "T", onClose: rec("close"), testid: "ov", selector: {SEARCHABLE}, footer: {{ right: null }} }})')
    form.eval('MR.click("tpl"); MR.find("tpl:search").props.onChange({ target: { value: "big" } }); MR.rerender();')
    form.eval('var PREVENTED = 0; MR.find("tpl:search").props.onKeyDown({ key: "Enter", preventDefault: function () { PREVENTED++; }, stopPropagation: function () {} }); MR.rerender();')
    assert js(form, "CALLS") == [["tpl", "k-big"]] and form.eval("PREVENTED") == 1
    assert form.eval('MR.find("tpl:menu") === null')


def test_k1_the_filter_box_sits_outside_the_menu_so_the_menu_holds_only_its_items(form) -> None:
    """Round 2 of #886, item 9: a text box is not a menu item, so it sits above the role="menu" list, in the popup that holds both, and names the list it
    filters (aria-controls). The "No match" line is outside the menu too."""
    form.eval(f'MR.mount(PK_FormOverlay, {{ title: "T", onClose: rec("close"), testid: "ov", selector: {SEARCHABLE}, footer: {{ right: null }} }})')
    form.eval('MR.click("tpl")')
    roles = js(form, 'ALL("tpl:menu").filter(function (n) { return n.props && n.props.role; }).map(function (n) { return n.props.role; })')
    assert roles == ["menu", "menuitemradio", "menuitemradio", "menuitemradio"]
    in_menu = js(form, 'ALL("tpl:menu").map(function (n) { return n.props && n.props["data-testid"]; }).filter(Boolean)')
    assert "tpl:search" not in in_menu
    assert "tpl:search" in js(form, 'ALL("tpl:popup").map(function (n) { return n.props && n.props["data-testid"]; }).filter(Boolean)')
    assert js(form, 'MR.find("tpl:search").props["aria-controls"]') == js(form, 'MR.find("tpl:menu").props.id')
    form.eval('MR.find("tpl:search").props.onChange({ target: { value: "zzz" } }); MR.rerender();')
    assert "No match" not in form.eval('textOf(MR.find("tpl:menu"))')
    assert js(form, 'ALL("tpl:menu").filter(function (n) { return n.props && n.props.role; }).map(function (n) { return n.props.role; })') == ["menu"]


def test_k1_the_arrow_keys_from_the_box_land_on_the_first_and_the_last_item(form) -> None:
    """Round 2 of #886, item 9: from the box (not an item), ArrowDown lands on the first item and ArrowUp on the LAST (it landed on the one before it);
    Home and End stay the box's own. From an item the keys wrap as before."""
    form.eval(
        "var FOCUSED = []; function item(n) { return { focus: function () { FOCUSED.push(n); } }; } var ITEMS = [item(0), item(1), item(2)];"
        " var INPUT = { tagName: 'INPUT' };"
        " function key(k, target) { var prevented = 0; PK_menuKeys({ key: k, target: target, currentTarget: { querySelectorAll: function () { return ITEMS; } },"
        " preventDefault: function () { prevented++; } }); return prevented; }"
    )
    form.eval("document.activeElement = INPUT;")
    assert form.eval("key('ArrowUp', INPUT)") == 1 and js(form, "FOCUSED") == [2]
    assert form.eval("key('ArrowDown', INPUT)") == 1 and js(form, "FOCUSED") == [2, 0]
    assert form.eval("key('Home', INPUT)") == 0 and form.eval("key('End', INPUT)") == 0 and js(form, "FOCUSED") == [2, 0]
    form.eval("document.activeElement = ITEMS[0];")
    form.eval("key('ArrowUp', ITEMS[0])")
    form.eval("document.activeElement = ITEMS[2];")
    form.eval("key('ArrowDown', ITEMS[2]); key('End', ITEMS[2]);")
    assert js(form, "FOCUSED") == [2, 0, 2, 0, 2]


def test_k1_a_selector_that_is_not_searchable_has_no_box(form) -> None:
    form.eval(f'MR.mount(PK_FormOverlay, {{ title: "T", onClose: rec("close"), testid: "ov", selector: {SELECTOR}, footer: {{ right: null }} }})')
    form.eval('MR.click("sel")')
    assert form.eval('MR.find("sel:search") === null')


def test_k4_a_disabled_segmented_control_shows_its_value_and_reports_no_pick(form) -> None:
    form.eval('MR.mount(PK_Segmented, { testid: "route", ariaLabel: "Routing", value: "seq", onChange: rec("seg"), disabled: true,'
              ' options: [{ value: "seq", label: "Sequential fallback" }, { value: "rr", label: "Round robin" }] })')
    assert form.eval('MR.find("route:seq").props["aria-pressed"]') == "true"
    assert form.eval('MR.find("route:rr").props.disabled') is True and form.eval('MR.find("route:seq").props.disabled') is True
    form.eval('MR.click("route:rr")')
    assert js(form, "CALLS") == []
    form.eval('MR.mount(PK_Segmented, { testid: "route", value: "seq", onChange: rec("seg"), options: [{ value: "seq", label: "S" }, { value: "rr", label: "R" }] })')
    assert form.eval('!MR.find("route:rr").props.disabled'), "without the prop the buttons are live, as before"


def test_k4_a_read_only_row_table_shows_its_rows_and_offers_no_change(form) -> None:
    form.eval(f'MR.mount(PK_RowTable, {{ columns: {COLUMNS}, rows: [{{ name: "llama", effect: "NoExecute" }}], onChange: rec("rows"), addLabel: "+ Add",'
              ' title: "TOLERATIONS", testid: "tbl", readOnly: true })')
    assert form.eval('MR.find("tbl:add") === null') and form.eval('MR.find("tbl:remove:0") === null')
    assert form.eval('MR.find("tbl:0:name").props.readOnly') is True and form.eval('MR.find("tbl:0:name").props.value') == "llama"
    assert form.eval('MR.find("tbl:0:effect").props.disabled') is True, "a select has no read-only state: it is disabled"
    assert "TOLERATIONS" in js(form, "MR.texts()")
    form.eval(f'MR.mount(PK_RowTable, {{ columns: {COLUMNS}, rows: [{{ name: "x", effect: "NoExecute" }}], onChange: rec("rows"), addLabel: "+ Add", testid: "tbl" }})')
    assert form.eval('!!MR.find("tbl:add")') and form.eval('!MR.find("tbl:0:name").props.readOnly'), "without the prop the table edits, as before"


def test_on_a_phone_the_overlay_is_a_sheet_with_the_kit_class_and_testid(form) -> None:
    form.eval('MOBILE = true; MR.mount(PK_FormOverlay, { title: "Register provider", onClose: rec("close"), testid: "pc-register", footer: { right: null } })')
    assert form.eval('MR.find("pc-register").props.className').split() == ["sheet", "pk-overlay"]
    assert form.eval('MR.find("pc-register").props.role') == "dialog"


# ---- the Modal's new props ----------------------------------------------------------------------------------------------------------------------------


def test_the_modal_keeps_its_markup_without_the_new_props(form) -> None:
    form.eval('MR.mount(host(el(Modal, { title: "Delete", onClose: rec("close") }, "body")))')
    dialogs = js(form, 'ALL("host").filter(function (n) { return n.props && n.props.role === "dialog"; }).map(function (n) {'
                       ' return { className: n.props.className, testid: n.props["data-testid"], width: n.props.style }; })')
    assert dialogs == [{"className": "modal"}], "no class and no test id: the markup is as before"
    form.eval('MOBILE = true; MR.mount(host(el(Modal, { title: "Delete", onClose: rec("close") }, "body")))')
    sheets = js(form, 'ALL("host").filter(function (n) { return n.props && n.props.role === "dialog"; }).map(function (n) {'
                      ' return { className: n.props.className, testid: n.props["data-testid"] }; })')
    assert sheets == [{"className": "sheet"}]


def test_the_modal_appends_a_class_and_takes_a_testid(form) -> None:
    form.eval('MR.mount(host(el(Modal, { title: "T", onClose: rec("close"), className: "x-wide", testid: "x-dialog" }, "body")))')
    assert form.eval('MR.find("x-dialog").props.className') == "modal x-wide"
    assert 'className={"modal" + (className ? " " + className : "")}' in SHARED_SRC
    assert 'className={"sheet" + (className ? " " + className : "")}' in SHARED_SRC


# ---- fields -----------------------------------------------------------------------------------------------------------------------------------------


def _field(form, props: str, child: str = 'el("input", { "data-testid": "f-url", className: "input mine", placeholder: "http://" })') -> None:
    form.eval(f"MR.mount(host(el(PK_Field, {props}, {child})))")


def test_a_field_draws_one_label_and_help_and_error_under_the_control(form) -> None:
    _field(form, '{ label: "URL", help: "Optional for local servers", error: "Required" }')
    nodes = js(form, 'ALL("host").filter(function (n) { return typeof n.type === "string"; }).map(function (n) {'
                     ' return { type: n.type, className: n.props.className || "", id: n.props.id, htmlFor: n.props.htmlFor, role: n.props.role,'
                     ' testid: n.props["data-testid"], describedby: n.props["aria-describedby"] }; })')
    labels = [n for n in nodes if n["type"] == "label"]
    assert len(labels) == 1 and labels[0]["className"] == "field-label pk-label"
    control = next(n for n in nodes if n.get("testid") == "f-url")
    assert labels[0]["htmlFor"] == control["id"], "the label names the control"
    helps = [n for n in nodes if n["className"] == "field-help" and n.get("role") != "alert"]
    errors = [n for n in nodes if n.get("role") == "alert"]
    assert len(helps) == 1 and len(errors) == 1
    order = [nodes.index(labels[0]), nodes.index(control), nodes.index(helps[0]), nodes.index(errors[0])]
    assert order == sorted(order), "label, control, help, error: the help and the error sit under the control"
    assert control["describedby"] == helps[0]["id"] + " " + errors[0]["id"]
    texts = js(form, "MR.texts()")
    assert "Optional for local servers" in texts and "Required" in texts
    field = next(n for n in nodes if "pk-field" in n["className"].split())
    assert field["className"].split() == ["field", "pk-field"]


def test_a_field_never_draws_an_inline_hint(form) -> None:
    _field(form, '{ label: "Name", hint: "optional" }')
    assert js(form, 'ALL("host").filter(function (n) { return n.props && /(^| )hint( |$)/.test(n.props.className || ""); }).length') == 0
    assert "optional" not in js(form, "MR.texts()"), "PK_Field never passes FormField's hint"
    assert form.eval('textOf(ALL("host").filter(function (n) { return n.type === "label"; })[0])') == "Name"


def test_span_full_and_required(form) -> None:
    _field(form, '{ label: "Description", span: "full", required: true }')
    field = form.eval('ALL("host").filter(function (n) { return /pk-field/.test(n.props && n.props.className || ""); })[0].props.className')
    assert field.split() == ["field", "pk-field", "pk-span-full"]
    marks = js(form, 'ALL("host").filter(function (n) { return n.props && n.props["aria-hidden"] === "true"; }).map(textOf)')
    assert marks == [" *"], "the required mark is drawn and hidden from assistive tech"
    assert form.eval('textOf(ALL("host").filter(function (n) { return n.type === "label"; })[0])') == "Description *"


def test_a_field_passes_its_children_through(form) -> None:
    _field(form, '{ label: "URL", help: "h" }')
    props = js(form, 'MR.find("f-url").props')
    assert props["className"] == "input mine" and props["placeholder"] == "http://" and props["data-testid"] == "f-url"
    assert set(props) == {"data-testid", "className", "placeholder", "id", "aria-describedby"}, "the control gains only FormField's id and description"


def test_grid_and_section_columns(form) -> None:
    form.eval('MR.mount(PK_Section, { title: "LIMITS AND RETRIES", cols: 2, testid: "limits" })')
    section = js(form, 'MR.find("limits").props')
    assert section["className"] == "pk-section" and section["role"] == "group"
    nodes = js(form, 'ALL("limits").filter(function (n) { return typeof n.type === "string"; }).map(function (n) {'
                     ' return { className: n.props.className, id: n.props.id, cols: n.props["data-cols"] }; })')
    title = next(n for n in nodes if n["className"] == "pk-section-title")
    assert section["aria-labelledby"] == title["id"], "the box is named by its title"
    assert next(n for n in nodes if n["className"] == "pk-form-grid")["cols"] == "2"
    assert "LIMITS AND RETRIES" in js(form, "MR.texts()")
    for cols, expected in (("undefined", "2"), ("1", "1"), ("3", "3"), ("7", "2")):
        form.eval(f'MR.mount(host(el(PK_Grid, {{ cols: {cols} }}, el("span", null, "x"))))')
        assert form.eval('ALL("host").filter(function (n) { return n.props && n.props.className === "pk-form-grid"; })[0].props["data-cols"]') == expected, cols


def test_fold_starts_closed_unless_open_and_toggles(form) -> None:
    form.eval('MR.mount(PK_Fold, { label: "More timeouts", testid: "fold", onToggle: rec("fold"), children: el("div", { "data-testid": "inner" }, "x") })')
    toggle = 'MR.find("fold:toggle")'
    assert form.eval(f"{toggle}.type") == "button" and form.eval(f"{toggle}.props.type") == "button"
    assert form.eval(f'{toggle}.props["aria-expanded"]') == "false"
    assert form.eval(f"textOf({toggle})") == "▸ More timeouts"
    assert form.eval('MR.find("inner") === null'), "the folded children are not mounted"
    form.eval('MR.click("fold:toggle")')
    assert form.eval(f'{toggle}.props["aria-expanded"]') == "true" and form.eval(f"textOf({toggle})") == "▾ More timeouts"
    assert form.eval('!!MR.find("inner")')
    form.eval('MR.click("fold:toggle")')
    assert js(form, "CALLS") == [["fold", True], ["fold", False]]
    assert form.eval('MR.find("inner") === null')
    # a fresh instance: the harness keeps a component's state at a position across mounts, so the root is emptied first (React mounts anew)
    form.eval('MR.mount(function Empty() { return null; }); MR.mount(PK_Fold, { label: "Advanced", open: true, testid: "adv", children: el("div", { "data-testid": "inner" }, "x") })')
    assert form.eval('!!MR.find("inner")'), "open: true starts open"
    form.eval('MR.rerender({ label: "Advanced", open: false, testid: "adv", children: el("div", { "data-testid": "inner" }, "x") })')
    assert form.eval('!!MR.find("inner")'), "open is the initial state only"


def test_segmented_marks_the_value_and_reports_a_pick(form) -> None:
    form.eval('MR.mount(PK_Segmented, { testid: "route", ariaLabel: "Routing", value: "seq", onChange: rec("seg"),'
              ' options: [{ value: "seq", label: "Sequential fallback" }, { value: "rr", label: "Round robin" }] })')
    root = js(form, 'MR.find("route").props')
    assert root["role"] == "group" and root["aria-label"] == "Routing" and root["className"] == "pk-seg"
    assert form.eval('MR.find("route:seq").props["aria-pressed"]') == "true"
    assert form.eval('MR.find("route:rr").props["aria-pressed"]') == "false"
    assert form.eval('MR.find("route:rr").type') == "button" and form.eval('MR.find("route:rr").props.type') == "button"
    form.eval('MR.click("route:rr")')
    assert js(form, "CALLS") == [["seg", "rr"]]
    form.eval('MR.click("route:seq")')
    assert js(form, "CALLS") == [["seg", "rr"]], "the pressed choice is already the value: picking it again reports nothing"
    form.eval('MR.rerender({ testid: "route", ariaLabel: "Routing", value: "rr", onChange: rec("seg"),'
              ' options: [{ value: "seq", label: "Sequential fallback" }, { value: "rr", label: "Round robin" }] })')
    assert form.eval('MR.find("route:rr").props["aria-pressed"]') == "true"
    form.eval('MR.click("route:seq")')
    assert js(form, "CALLS") == [["seg", "rr"], ["seg", "seq"]]


# ---- the tables -------------------------------------------------------------------------------------------------------------------------------------

COLUMNS = ('[{ key: "name", label: "Model", input: "text" },'
           ' { key: "effect", label: "Effect", input: "select", options: ["NoSchedule", "NoExecute"] }]')


def test_row_table_adds_edits_and_removes_rows_with_named_controls(form) -> None:
    form.eval(f'MR.mount(PK_RowTable, {{ columns: {COLUMNS}, rows: [], onChange: rec("rows"), addLabel: "+ Add", title: "TOLERATIONS", testid: "tbl" }})')
    assert form.eval('MR.find("tbl:add").props.children') == "+ Add"
    assert "TOLERATIONS" in js(form, "MR.texts()")
    form.eval('MR.click("tbl:add")')
    assert js(form, "CALLS") == [["rows", [{"name": "", "effect": "NoSchedule"}]]], "a select column starts on its first option"
    form.eval(f'MR.mount(PK_RowTable, {{ columns: {COLUMNS}, rows: [{{ name: "", effect: "NoSchedule" }}], onChange: rec("rows"), addLabel: "+ Add", testid: "tbl" }})')
    assert form.eval('MR.find("tbl:0:name").props["aria-label"]') == "Model 1"
    assert form.eval('MR.find("tbl:0:effect").props["aria-label"]') == "Effect 1"
    assert form.eval('MR.find("tbl:0:effect").type') == "select"
    form.eval('MR.find("tbl:0:name").props.onChange({ target: { value: "llama" } }); MR.find("tbl:0:effect").props.onChange({ target: { value: "NoExecute" } });')
    assert js(form, "CALLS")[-2:] == [["rows", [{"name": "llama", "effect": "NoSchedule"}]], ["rows", [{"name": "", "effect": "NoExecute"}]]]
    assert form.eval('MR.find("tbl:remove:0").props["aria-label"]') == "Remove row 1"
    form.eval('MR.click("tbl:remove:0")')
    assert js(form, "CALLS")[-1] == ["rows", []]


def test_row_table_secret_columns_are_password_inputs(form) -> None:
    form.eval('MR.mount(PK_RowTable, { columns: [{ key: "name", label: "Name", input: "text" }, { key: "value", label: "Value", input: "secret" },'
              ' { key: "n", label: "Count", input: "number" }], rows: [{ name: "A", value: "x", n: "2" }], onChange: rec("rows"), addLabel: "+ Add", testid: "env" })')
    assert form.eval('MR.find("env:0:value").props.type') == "password"
    assert form.eval('MR.find("env:0:value").props.autoComplete') == "off"
    assert form.eval('MR.find("env:0:value").props.value') == "x"
    assert form.eval('MR.find("env:0:name").props.type') == "text"
    assert form.eval('MR.find("env:0:n").props.type') == "number"


def test_check_table_toggles_one_and_all(form) -> None:
    rows = '[{ key: "m1", label: "llama", checked: true, extra: el("input", { "data-testid": "ctx-m1" }) }, { key: "m2", label: "qwen", checked: false }]'
    form.eval(f'MR.mount(PK_CheckTable, {{ rows: {rows}, onToggle: rec("one"), onToggleAll: rec("all"), extraLabel: "Context window", testid: "ct" }})')
    assert form.eval('MR.find("ct:all").props.checked') is False
    assert form.eval('MR.find("ct:all").props["aria-label"]') == "Select all"
    assert form.eval('MR.find("ct:m1").props["aria-label"]') == "llama" and form.eval('MR.find("ct:m1").props.checked') is True
    assert "Context window" in js(form, "MR.texts()")
    assert form.eval('!!MR.find("ctx-m1")'), "a row's extra is drawn in its last column"
    form.eval('MR.find("ct:all").props.onChange({ target: { checked: true } });')
    assert js(form, "CALLS") == [["all", True, ["m1", "m2"]]]
    form.eval('MR.find("ct:m2").props.onChange({ target: { checked: true } });')
    assert js(form, "CALLS")[-1] == ["one", "m2"]
    form.eval(f'MR.mount(PK_CheckTable, {{ rows: {rows.replace("checked: false", "checked: true")}, onToggle: rec("one"), onToggleAll: rec("all"), testid: "ct" }})')
    assert form.eval('MR.find("ct:all").props.checked') is True
    form.eval('MR.find("ct:all").props.onChange({ target: { checked: false } });')
    assert js(form, "CALLS")[-1] == ["all", False, ["m1", "m2"]]


def test_a_disabled_check_table_row_cannot_be_toggled_is_left_out_of_toggle_all_and_is_muted(form) -> None:
    """The lead's addition for Plan 02 (a model already imported is listed, ticked and disabled)."""
    rows = ('[{ key: "m1", label: "llama", checked: true }, { key: "m2", label: "qwen", checked: false, disabled: true },'
            ' { key: "m3", label: "phi", checked: true }]')
    form.eval(f'MR.mount(PK_CheckTable, {{ rows: {rows}, onToggle: rec("one"), onToggleAll: rec("all"), testid: "ct" }})')
    assert form.eval('MR.find("ct:m2").props.disabled') is True
    assert form.eval('MR.find("ct:m1").props.disabled') is False
    form.eval('MR.find("ct:m2").props.onChange({ target: { checked: true } });')
    assert js(form, "CALLS") == [], "a disabled row cannot be toggled"
    assert form.eval('MR.find("ct:all").props.checked') is True, "every row that can be ticked is: the disabled one does not hold the header back"
    form.eval('MR.find("ct:all").props.onChange({ target: { checked: false } });')
    assert js(form, "CALLS") == [["all", False, ["m1", "m3"]]], "toggle-all names the rows it applies to, and the disabled one is not among them"
    rows_drawn = js(form, 'ALL("ct").filter(function (n) { return n.props && /pk-checktable-row/.test(n.props.className || ""); }).map(function (n) {'
                          ' return n.props["data-disabled"] || null; })')
    assert rows_drawn.count("true") == 1, "the disabled row says so, for its muted style"
    muted = declarations(css_body('.pk-checktable-row[data-disabled="true"]'))
    assert muted.get("color") == "var(--text-4)"
    form.eval('MR.mount(PK_CheckTable, { rows: [{ key: "m2", label: "qwen", checked: true, disabled: true }], onToggle: rec("one"), onToggleAll: rec("all"), testid: "ct" })')
    assert form.eval('MR.find("ct:all").props.disabled') is True, "nothing to toggle"
    form.eval('MR.find("ct:all").props.onChange({ target: { checked: false } });')
    assert js(form, "CALLS") == [["all", False, ["m1", "m3"]]]


# ---- CSS and exports --------------------------------------------------------------------------------------------------------------------------------


def test_the_overlay_uses_no_transform() -> None:
    """A transformed box is the containing block of every ``position: fixed`` element inside it, so a full-screen editor in an overlay (Plan 05) would
    cover the dialog instead of the viewport. Neither the dialog, its scrim, the keyframes they run nor any kit overlay rule may transform."""
    rules = css_rules()
    watched = [(selectors, body) for at, selectors, body in rules
               if at is None and any(s in (".modal", ".modal-overlay") or ".pk-overlay" in s for s in selectors)]
    assert watched, "the dialog rules were found"
    assert not [s for s, body in watched if TRANSFORM.search(body)]
    names = {name for _s, body in watched for name in re.findall(r"animation:\s*([\w-]+)", body)}
    for at, _selectors, body in rules:
        if at and at.split()[0] == "@keyframes" and at.split()[1] in names:
            assert not TRANSFORM.search(body), at


def test_the_form_grid_stacks_below_520px() -> None:
    assert declarations(css_body(".pk-form-grid", "@media (max-width: 520px)"))["grid-template-columns"] == "1fr"


def test_the_form_kit_css() -> None:
    for selector in ('.pk-field > input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="file"])', ".pk-field > select"):
        d = declarations(css_body(selector))
        assert d.get("height") == "34px" and d.get("box-sizing") == "border-box" and d.get("width") == "100%", selector
    label = declarations(css_body(".pk-label"))
    assert label.get("white-space") == "nowrap" and label.get("overflow") == "hidden" and label.get("text-overflow") == "ellipsis", "a label sits on one line"
    grid = declarations(css_body(".pk-form-grid"))
    assert grid["display"] == "grid" and grid["grid-template-columns"] == "repeat(var(--cols), minmax(0, 1fr))"
    assert grid["gap"] == "14px 12px" and grid["align-items"] == "start", "the controls of a row share their top edge"
    for n in ("1", "2", "3"):
        assert declarations(css_body(f'.pk-form-grid[data-cols="{n}"]'))["--cols"] == n
    assert declarations(css_body(".pk-span-full"))["grid-column"] == "1 / -1"
    section = declarations(css_body(".pk-section"))
    assert section["border-radius"] == "var(--r-10)" and section["border"].startswith("1px solid")
    title = declarations(css_body(".pk-section-title"))
    assert (title["font-size"], title["text-transform"], title["letter-spacing"], title["color"]) == ("11px", "uppercase", "0.06em", "var(--text-4)")
    head = declarations(css_body(".pk-overlay .modal-h"))
    assert head["justify-content"] == "center" and head["position"] == "relative", "the title is centred"
    close = declarations(css_body(".pk-overlay .modal-h .close"))
    assert close["position"] == "absolute" and "right" in close, "the close button sits on the right"
    foot = declarations(css_body(".pk-foot"))
    assert (foot["display"], foot["width"], foot["justify-content"]) == ("flex", "100%", "space-between"), "Cancel left, the action right"
    assert declarations(css_body(".pk-checktable-body"))["max-height"] == "320px"


def test_the_overlay_body_scrolls_instead_of_shrinking_its_blocks() -> None:
    """The overlay body is a flex column that scrolls. Found in a browser render: without this, its blocks shrank to fit the dialog, and a row
    table or check table (overflow: hidden, so no minimum height) collapsed to a line."""
    for selector in (".pk-overlay .modal-b > *", ".pk-overlay .sheet-b > *"):
        assert declarations(css_body(selector)).get("flex-shrink") == "0", selector
    body = declarations(css_body(".pk-overlay .modal-b"))
    assert body.get("display") == "flex" and body.get("flex-direction") == "column"


def test_the_segmented_control_is_as_wide_as_its_choices() -> None:
    """A flex column or a grid cell stretches an auto-width box: the segmented control drew as a full-width bar with its choices on the left."""
    seg = declarations(css_body(".pk-seg"))
    assert seg.get("width") == "max-content" and seg.get("max-width") == "100%"


def test_the_kit_overlays_close_button_is_centred_and_tap_sized_on_a_phone() -> None:
    close = declarations(css_body(".pk-overlay .sheet-h .close"))
    assert close.get("width") == "var(--tap-min)" and close.get("height") == "var(--tap-min)"
    shared = declarations(css_body(".pk-overlay .modal-h .close"))
    assert (shared.get("top"), shared.get("bottom"), shared.get("margin")) == ("0", "0", "auto 0"), "centred without a transform"
    assert int(declarations(css_body(".pk-overlay .sheet-h"))["min-height"].rstrip("px")) >= 44 + 8, "the header holds the 44px button"


def test_every_form_export_is_on_window(form) -> None:
    for name in ("PK_FormOverlay", "PK_Grid", "PK_Field", "PK_Section", "PK_Fold", "PK_Segmented", "PK_RowTable", "PK_CheckTable",
                 "PK_PageHeader", "PK_CardGrid", "PK_Card", "PK_usePagedFilter"):
        assert form.eval(f"typeof window.{name}") == "function", name
