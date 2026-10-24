"""The LLM profile overlays, rendered (Platform revamp Plan 03, Tasks 2 and 3).

``MP_ProfileModal`` is a hook-free dispatcher that ``NV_ProfileModalHost`` mounts; it hands the profile to ``MP_AggregateOverlay`` (create, or edit of a
``kind: "aggregated"`` row) or to ``MP_SingleProfileOverlay`` (anything else). Both are built from the platform kit, and both are driven here through the
kit's own contract: the REAL ``shared.jsx`` (``Modal``, ``Btn``), ``shared/form-field.jsx``, ``shared/platform-kit.jsx``, ``foundation/api.js`` and
``model-profiles.jsx``, transpiled the way the server bundles them, on the hook runtime of ``tests/ui/_mini_react.py``. The prelude stubs only what the
console around an overlay provides (the Escape stack, the focus trap, ``useResource``, ``apiFetch`` once ``api.js`` has loaded, ``toastPush``); no ``PK_``
component is replaced, so a kit component reached with ``MR.findProp("testid", id)`` is the real one and its callbacks are the ones the overlay handed it.

The refusals the overlays are fed are the bodies the real router answers (``tests/ui/_llm_profile_fixtures.py``), not shapes anybody remembered.

The harness has no scheduler: a handler that awaits ``apiFetch`` finishes at the end of the ``eval`` that started it, and the tree is drawn again by the
next ``MR.rerender()`` (``settle``). It also keeps an instance's state while the same component sits at the same position, so ``mount`` starts every
scenario from an empty tree.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._llm_profile_fixtures import FULL_AGG, FULL_SINGLE, PROVIDERS, ROWS, router_envelopes
from tests.ui._mini_react import ROOT, mini_react_context, transpile

UI = ROOT / "ui"
SOURCE = (UI / "components" / "model-profiles.jsx").read_text(encoding="utf-8")

RULE = "Use letters, digits, dots, dashes and underscores, and start with a letter or a digit."
HARNESS_NOTE = "Managed by harness h1. Re-run the harness to change it."
TAKEN = "ModelProfile with id 'p-a' already exists"
GONE = "member profile 'p-gone' does not exist"
WHOLE = "Enter a whole number of tokens, 1 or more."
FOOTNOTE = "Agents bind to a profile. A session, a binding switch or a graph node can override it. Deleting is refused while anything uses it."

PRELUDE = r"""
var CALLS = [], TOASTS = [], SAVED = [], CLOSED = [], RETRIES = [], PENDING = [], ASKED = [];
var LIST = { data: { items: [] }, error: null, loading: false };   // what window.primerApi.useResource answers
var ANSWER = null;                                                  // when set, apiFetch resolves with whatever it returns
var document = { addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.primerApi = {
  useEscape: function () {},
  useFocusTrap: function () {},
  useResource: function (key, fetcher, opts) {
    ASKED.push({ key: key, fetcher: fetcher, opts: opts || null });
    return { data: LIST.data, error: LIST.error, loading: LIST.loading, degraded: false, refetch: function () { RETRIES.push(1); } };
  },
  toastPush: function (t) { TOASTS.push(t); },
};
"""

# after api.js has loaded: it sets the real apiFetch, ApiError and readRefusal on window.primerApi, and only apiFetch is replaced
AFTER = r"""
window.primerApi.apiFetch = function (method, path, body) {
  CALLS.push([method, path, body === undefined ? null : JSON.parse(JSON.stringify(body))]);
  return ANSWER ? ANSWER() : Promise.resolve(body);
};
function mount(props) {
  MR.mount(MP_ProfileModal, Object.assign({
    open: true, existing: null, providers: [],
    onSaved: function (row) { SAVED.push(row); }, onClose: function () { CLOSED.push(1); },
  }, props || {}));
}
function typeInto(testid, value) {
  var el = MR.find(testid);
  if (!el) throw new Error("no element with data-testid " + testid);
  el.props.onChange({ target: { value: value } });
  MR.rerender();
}
function viaProp(testid, name, arg) {
  var el = MR.findProp("testid", testid);
  if (!el) throw new Error("no component with testid " + testid);
  el.props[name](arg);
  MR.rerender();
}
function allUnder(testid) {
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
function textAt(testid) { var el = MR.find(testid); return el ? textOf(el) : null; }
function unlabelled(testid) {
  var nodes = allUnder(testid);
  var labelled = {};
  nodes.forEach(function (n) { if (n.type === "label" && n.props.htmlFor) labelled[n.props.htmlFor] = true; });
  return nodes.filter(function (n) {
    return (n.type === "input" || n.type === "select" || n.type === "textarea") && !(n.props.id && labelled[n.props.id]);
  }).map(function (n) { return n.props["data-testid"] || n.type; });
}
function fieldError(label) { var f = MR.findProp("label", label); return f ? (f.props.error || null) : "no such field"; }
// The <button> a Btn draws, pressed as a click on the page reaches it. MR.click calls the onClick of the Btn COMPONENT, which is the overlay's own handler; a busy Btn hands the
// button underneath a refusal instead, and that is the one a person's press reaches.
function pressHost(testid) {
  var host = allUnder(testid).filter(function (n) { return n.type === "button"; })[0];
  if (!host) throw new Error("no button under " + testid);
  host.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} });
  MR.rerender();
}
"""

FILES = (UI / "components" / "shared.jsx", UI / "components" / "shared" / "form-field.jsx", UI / "components" / "shared" / "platform-kit.jsx")


def js(ctx, expr: str):
    """The value of a JS expression as Python data; ``undefined`` (a prop that was not set) is ``None``, as ``null`` is."""
    return json.loads(ctx.eval(f"(function () {{ var v = ({expr}); return JSON.stringify(v === undefined ? null : v); }})()"))


@pytest.fixture(scope="module")
def code() -> str:
    parts = [transpile(p) for p in FILES]
    parts.append((UI / "foundation" / "api.js").read_text(encoding="utf-8"))
    parts.append(transpile(UI / "components" / "model-profiles.jsx"))
    return "\n;\n".join(parts)


@pytest.fixture
def ctx(code):
    c = mini_react_context(code, PRELUDE)
    c.eval(AFTER)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def envelopes(fake_storage_provider) -> dict:
    return router_envelopes(fake_storage_provider)


# ---- drivers ------------------------------------------------------------------------------------------------------------------------------------------


def set_rows(ctx, rows) -> None:
    ctx.eval(f"LIST = {{ data: {{ items: {json.dumps(rows)} }}, error: null, loading: false }};")


def mount(ctx, props: dict | None = None, rows=ROWS) -> None:
    """Mount ``MP_ProfileModal`` open, from an empty tree."""
    set_rows(ctx, rows)
    ctx.eval(f"MR.mount(function Nothing() {{ return null; }}, {{}}); mount({json.dumps(props or {})})")


def type_(ctx, testid: str, value: str) -> None:
    ctx.eval(f"typeInto({json.dumps(testid)}, {json.dumps(value)})")


def click(ctx, testid: str) -> None:
    ctx.eval(f"MR.click({json.dumps(testid)})")


def toggle(ctx, key: str) -> None:
    ctx.eval(f'viaProp("mp-aggregate-members", "onToggle", {json.dumps(key)})')


def segment(ctx, testid: str, value: str) -> None:
    ctx.eval(f"viaProp({json.dumps(testid)}, 'onChange', {json.dumps(value)})")


def settle(ctx) -> None:
    ctx.eval("MR.rerender()")


def table_keys(ctx) -> list[str]:
    return js(ctx, 'MR.findProp("testid", "mp-aggregate-members").props.rows.map(function (r) { return r.key; })')


def ticked(ctx) -> list[str]:
    return js(ctx, 'MR.findProp("testid", "mp-aggregate-members").props.rows.filter(function (r) { return r.checked; }).map(function (r) { return r.key; })')


def calls(ctx) -> list:
    return js(ctx, "CALLS")


def toasts(ctx) -> list:
    return js(ctx, "TOASTS")


def saved_count(ctx) -> int:
    return len(js(ctx, "SAVED"))


def closed_count(ctx) -> int:
    return len(js(ctx, "CLOSED"))


def text_at(ctx, testid: str):
    return js(ctx, f"textAt({json.dumps(testid)})")


def present(ctx, testid: str) -> bool:
    return bool(js(ctx, f"MR.find({json.dumps(testid)}) !== null"))


def prop(ctx, testid: str, name: str):
    return js(ctx, f"MR.find({json.dumps(testid)}).props[{json.dumps(name)}]")


def field_error(ctx, label: str):
    return js(ctx, f"fieldError({json.dumps(label)})")


def title(ctx) -> str:
    return prop(ctx, "mp-aggregate", "aria-label")


def reject_with(ctx, envelope: dict) -> None:
    ctx.eval(f"ANSWER = function () {{ return Promise.reject(new window.primerApi.ApiError({json.dumps(envelope)})); }};")


def pick_two(ctx) -> None:
    toggle(ctx, "p-a")
    toggle(ctx, "p-b")


def submit_and_settle(ctx) -> None:
    click(ctx, "mp-aggregate-submit")
    settle(ctx)


def advanced_open(ctx) -> bool:
    """Whether the Advanced fold is open: its toggle says so, and only an open fold mounts its two choices."""
    expanded = js(ctx, 'MR.find("mp-aggregate-advanced:toggle").props["aria-expanded"]') == "true"
    assert expanded == present(ctx, "mp-aggregate-failover-point") == present(ctx, "mp-aggregate-failover-on"), "an open fold shows both choices and a shut one neither"
    return expanded


def open_advanced(ctx) -> None:
    """Open the fold if it is shut. Whether it starts open depends on the row (an edit that holds a non-default choice opens it), and a press on an open one would shut it."""
    if not advanced_open(ctx):
        click(ctx, "mp-aggregate-advanced:toggle")
    assert advanced_open(ctx)


def aggregate_row(ctx, **routing) -> dict:
    """``FULL_AGG`` with its two failover choices at the model's defaults, unless a keyword says otherwise."""
    defaults = js(ctx, "MP_AGGREGATE_DEFAULTS")
    return {**FULL_AGG, "failover_point": defaults["failover_point"], "failover_on": defaults["failover_on"], **routing}


def assert_not_alphabetical(order: list[str]) -> None:
    """A scenario whose expected order is alphabetical, either way, cannot tell a payload that keeps the order on screen from one that sorts its members. Refuse to run on one."""
    assert order != sorted(order) and order != sorted(order, reverse=True), f"{order} is an alphabetical order"


def hold_the_request(ctx) -> None:
    """The next request stays out until ``PENDING[0]`` answers it."""
    ctx.eval("ANSWER = function () { return new Promise(function (resolve) { PENDING.push(resolve); }); };")


def dismiss(ctx, cancel: str) -> None:
    """The person closes the overlay: Cancel tells the host (``onClose``), and the host takes the overlay off the screen."""
    click(ctx, cancel)
    ctx.eval("MR.rerender({ open: false, existing: null, providers: [], onSaved: function () {}, onClose: function () {} })")


def press_host(ctx, testid: str) -> None:
    """Press the real ``<button>`` a ``Btn`` draws (``pressHost``): a busy ``Btn`` has swapped its handler for a refusal there."""
    ctx.eval(f"pressHost({json.dumps(testid)})")


# ---- create --------------------------------------------------------------------------------------------------------------------------------------------


def test_create_posts_the_members_in_the_order_shown(ctx) -> None:
    """The order IS the failover chain, so the body carries the order on screen. p-b, p-a and p-c are ticked in that order and p-c is moved up once: [p-b, p-c, p-a]. That is neither
    alphabetical order nor the order they were ticked in, so a payload that sorts its members, or sends them as ticked, is not the one on screen."""
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "fast-with-fallback")
    toggle(ctx, "p-b")
    toggle(ctx, "p-a")
    toggle(ctx, "p-c")
    click(ctx, "mp-member-up:p-c")
    segment(ctx, "mp-aggregate-routing", "round_robin")
    shown = ["p-b", "p-c", "p-a"]
    assert_not_alphabetical(shown)
    assert table_keys(ctx) == shown, "the picks in the order put in, and the aggregate in the list (agg-x) is not a candidate"
    click(ctx, "mp-aggregate-submit")
    assert calls(ctx) == [[
        "POST", "/model_profiles", {
            "id": "fast-with-fallback", "description": "Aggregate of p-b, p-c, p-a", "kind": "aggregated",
            "members": shown, "strategy": "round_robin",
            "failover_point": "before_first_token", "failover_on": "transient_and_config",
        },
    ]]
    assert toasts(ctx) == [{"kind": "success", "text": "Created aggregated profile fast-with-fallback"}]
    assert saved_count(ctx) == 1 and closed_count(ctx) == 1


def test_the_row_the_server_answers_is_what_onsaved_gets(ctx) -> None:
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "x")
    pick_two(ctx)
    ctx.eval('ANSWER = function () { return Promise.resolve({ id: "x", from: "server" }); };')
    click(ctx, "mp-aggregate-submit")
    assert js(ctx, "SAVED") == [{"id": "x", "from": "server"}]


def test_create_stays_off_until_a_valid_name_and_two_members(ctx) -> None:
    mount(ctx)
    submit = 'MR.find("mp-aggregate-submit").props'
    assert js(ctx, f"{submit}.disabled") is True, "nothing typed, nothing picked"
    type_(ctx, "mp-aggregate-name", "a/b")
    pick_two(ctx)
    assert js(ctx, f"{submit}.disabled") is True
    assert RULE in js(ctx, "MR.texts()"), "the rule is on screen"
    type_(ctx, "mp-aggregate-name", "ok")
    toggle(ctx, "p-b")
    assert js(ctx, f"{submit}.disabled") is True, "one member is not enough"
    assert "Pick at least two profiles." in js(ctx, "MR.texts()")
    toggle(ctx, "p-b")
    assert js(ctx, f"{submit}.disabled") is False
    assert "Pick at least two profiles." not in js(ctx, "MR.texts()")
    assert js(ctx, f'{submit}["aria-busy"]') is None, "it is not busy before the press"
    assert js(ctx, f"{submit}.busy") is False


def test_a_blank_name_is_not_an_error_until_something_is_typed(ctx) -> None:
    mount(ctx)
    assert field_error(ctx, "Name") is None
    type_(ctx, "mp-aggregate-name", "bad name")
    assert field_error(ctx, "Name") == RULE
    type_(ctx, "mp-aggregate-name", "")
    assert field_error(ctx, "Name") is None, "a cleared field is the empty form again, not a mistake"


def test_zero_members_say_nothing_and_one_says_pick_two(ctx) -> None:
    mount(ctx)
    assert field_error(ctx, "Members") is None
    toggle(ctx, "p-a")
    assert field_error(ctx, "Members") == "Pick at least two profiles."
    assert js(ctx, 'MR.findProp("label", "Members").props.help') == "Tick two or more. The order is the fallback order, or the rotation order for Round robin."


def test_the_window_line_follows_the_picks(ctx) -> None:
    mount(ctx)
    assert text_at(ctx, "mp-aggregate-window") == "Pick the profiles first."
    pick_two(ctx)
    assert text_at(ctx, "mp-aggregate-window") == "8,192 (the smallest of the picked profiles)"


def test_select_all_ticks_every_single_profile_after_the_ones_already_ticked(ctx) -> None:
    mount(ctx)
    toggle(ctx, "p-b")
    ctx.eval('viaProp("mp-aggregate-members", "onToggleAll", true)')
    assert ticked(ctx) == ["p-b", "p-a", "p-c"]
    assert table_keys(ctx) == ["p-b", "p-a", "p-c"]
    ctx.eval('viaProp("mp-aggregate-members", "onToggleAll", false)')
    assert ticked(ctx) == []


def test_up_and_down_reorder_and_the_ends_say_aria_disabled_never_disabled(ctx) -> None:
    mount(ctx)
    for key in ("p-a", "p-b", "p-c"):
        toggle(ctx, key)
    up_first, down_last = 'MR.find("mp-member-up:p-a").props', 'MR.find("mp-member-down:p-c").props'
    assert js(ctx, f'{up_first}["aria-disabled"]') == "true" and js(ctx, f'{down_last}["aria-disabled"]') == "true"
    assert js(ctx, f"{up_first}.disabled") is None and js(ctx, f"{down_last}.disabled") is None
    assert js(ctx, 'MR.find("mp-member-up:p-b").props["aria-disabled"]') is None
    assert js(ctx, f'{up_first}["aria-label"]') == "Move p-a up" and js(ctx, f'{down_last}["aria-label"]') == "Move p-c down"
    click(ctx, "mp-member-up:p-a")
    click(ctx, "mp-member-down:p-c")
    assert ticked(ctx) == ["p-a", "p-b", "p-c"], "a press at an end moves nothing"
    click(ctx, "mp-member-down:p-a")
    assert ticked(ctx) == ["p-b", "p-a", "p-c"]
    assert [text_at(ctx, f"mp-member-order:{k}") for k in ("p-b", "p-a", "p-c")] == ["1", "2", "3"]
    click(ctx, "mp-member-up:p-c")
    assert ticked(ctx) == ["p-b", "p-c", "p-a"]


def test_every_row_shows_its_window_and_only_a_ticked_row_shows_an_order(ctx) -> None:
    mount(ctx)
    toggle(ctx, "p-b")
    assert text_at(ctx, "mp-member-window:p-a") == "32,768"
    assert text_at(ctx, "mp-member-window:p-b") == "8,192"
    assert text_at(ctx, "mp-member-window:p-c") == "131,072"
    assert present(ctx, "mp-member-order:p-b") and not present(ctx, "mp-member-order:p-a")
    assert not present(ctx, "mp-member-up:p-a") and present(ctx, "mp-member-up:p-b")
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-members").props.extraLabel') == "Context window and order"


def test_routing_help_follows_the_choice_and_the_advanced_fold_holds_the_failover_choices(ctx) -> None:
    mount(ctx)
    assert js(ctx, 'MR.findProp("label", "Routing").props.help') == "Always try profile 1 first; if it answers 429, a 5xx or times out, move to the next."
    segment(ctx, "mp-aggregate-routing", "round_robin")
    assert js(ctx, 'MR.findProp("label", "Routing").props.help') == "Rotate which profile goes first on each call."
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-routing").props.ariaLabel') == "Routing"
    assert not present(ctx, "mp-aggregate-failover-point"), "the Advanced fold starts folded and mounts nothing"
    click(ctx, "mp-aggregate-advanced:toggle")
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-point").props.value') == "before_first_token"
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-on").props.value') == "transient_and_config", "the model's default, not the stricter one"
    assert js(ctx, 'MR.findProp("label", "Fail over").props.help') == "Failing over mid-stream can repeat text the user has already seen."
    assert js(ctx, 'MR.findProp("label", "Fail over on").props.help') == "Transient errors are 429, 5xx, timeouts and network failures."
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-point").props.ariaLabel') == "Fail over"
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-on").props.ariaLabel') == "Fail over on"
    segment(ctx, "mp-aggregate-failover-point", "mid_stream")
    segment(ctx, "mp-aggregate-failover-on", "transient")
    click(ctx, "mp-aggregate-advanced:toggle")
    click(ctx, "mp-aggregate-advanced:toggle")
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-point").props.value') == "mid_stream", "folding it away does not forget the choice"
    type_(ctx, "mp-aggregate-name", "adv")
    pick_two(ctx)
    click(ctx, "mp-aggregate-submit")
    body = calls(ctx)[0][2]
    assert (body["strategy"], body["failover_point"], body["failover_on"]) == ("round_robin", "mid_stream", "transient")


# The lead's ruling for the Advanced fold: folded on a create; on an edit it starts open when a choice inside it is not the model's default, so a person sees what is set; a harness-managed row
# keeps it open. `open` is the kit's initial-state-only prop, so each case below is a fresh mount.


def test_the_advanced_fold_is_folded_on_a_create(ctx) -> None:
    mount(ctx)
    assert advanced_open(ctx) is False


def test_the_advanced_fold_is_folded_on_an_edit_that_holds_the_defaults(ctx) -> None:
    mount(ctx, {"existing": aggregate_row(ctx)})
    assert advanced_open(ctx) is False


def test_the_advanced_fold_opens_on_an_edit_with_a_non_default_failover_point(ctx) -> None:
    mount(ctx, {"existing": aggregate_row(ctx, failover_point="mid_stream")})
    assert advanced_open(ctx) is True
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-point").props.value') == "mid_stream"
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-on").props.value') == "transient_and_config"


def test_the_advanced_fold_opens_on_an_edit_with_a_non_default_failover_on(ctx) -> None:
    mount(ctx, {"existing": aggregate_row(ctx, failover_on="transient")})
    assert advanced_open(ctx) is True
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-point").props.value') == "before_first_token"
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-on").props.value') == "transient"


def test_the_advanced_fold_is_folded_on_a_row_with_no_failover_fields(ctx) -> None:
    bare = {k: v for k, v in FULL_AGG.items() if k not in ("failover_point", "failover_on")}
    mount(ctx, {"existing": bare})
    assert advanced_open(ctx) is False
    open_advanced(ctx)
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-point").props.value') == "before_first_token", "an absent field reads as the model's default"
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-on").props.value') == "transient_and_config"


def test_a_managed_aggregate_keeps_its_fold_open_even_when_it_holds_the_defaults(ctx) -> None:
    """Its toggle is disabled with the rest of the form, so a shut fold could not be read."""
    mount(ctx, {"existing": aggregate_row(ctx, harness_id="h1")})
    assert advanced_open(ctx) is True


def test_the_overlay_is_the_kit_form_overlay_720_wide_with_a_title_and_the_buttons_of_its_mode(ctx) -> None:
    mount(ctx)
    assert prop(ctx, "mp-aggregate", "role") == "dialog" and prop(ctx, "mp-aggregate", "className") == "modal pk-overlay"
    assert prop(ctx, "mp-aggregate", "style") == {"width": 720}
    assert title(ctx) == "New aggregated profile" and "New aggregated profile" in js(ctx, "MR.texts()")
    assert text_at(ctx, "mp-aggregate-submit") == "Create" and text_at(ctx, "mp-aggregate-cancel") == "Cancel"
    mount(ctx, {"existing": FULL_AGG})
    assert title(ctx) == "Edit aggregated profile"
    assert text_at(ctx, "mp-aggregate-submit") == "Save" and text_at(ctx, "mp-aggregate-cancel") == "Cancel"


def test_cancel_closes_without_a_request(ctx) -> None:
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "draft")
    click(ctx, "mp-aggregate-cancel")
    assert closed_count(ctx) == 1 and saved_count(ctx) == 0 and calls(ctx) == []


# ---- a refused save ------------------------------------------------------------------------------------------------------------------------------------


def test_a_refused_create_keeps_the_draft_and_names_the_field(ctx, envelopes) -> None:
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "fast-with-fallback")
    pick_two(ctx)

    reject_with(ctx, envelopes["conflict"])
    submit_and_settle(ctx)
    assert field_error(ctx, "Name") == TAKEN and field_error(ctx, "Members") is None
    assert toasts(ctx) == [] and closed_count(ctx) == 0 and saved_count(ctx) == 0, "a refusal is drawn in the overlay, never toasted"
    assert prop(ctx, "mp-aggregate-name", "value") == "fast-with-fallback" and ticked(ctx) == ["p-a", "p-b"]
    assert js(ctx, 'MR.find("mp-aggregate-submit").props.busy') is False, "the button is usable again"

    reject_with(ctx, envelopes["member_not_found"])
    submit_and_settle(ctx)
    assert field_error(ctx, "Members") == GONE and field_error(ctx, "Name") is None, "a new press starts from a clean form"
    assert toasts(ctx) == [] and closed_count(ctx) == 0 and saved_count(ctx) == 0
    assert prop(ctx, "mp-aggregate-name", "value") == "fast-with-fallback" and ticked(ctx) == ["p-a", "p-b"]


def test_a_refusal_the_form_has_no_field_for_is_drawn_above_the_footer(ctx, envelopes) -> None:
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "x")
    pick_two(ctx)
    reject_with(ctx, envelopes["provider_not_found"])
    submit_and_settle(ctx)
    assert prop(ctx, "mp-aggregate-error", "role") == "alert"
    assert "no-such-provider" in text_at(ctx, "mp-aggregate-error")
    assert field_error(ctx, "Name") is None and field_error(ctx, "Members") is None
    assert closed_count(ctx) == 0
    body = js(ctx, 'allUnder("mp-aggregate").map(function (n) { return n.props["data-testid"] || n.props.className || null; }).filter(Boolean)')
    assert body.index("mp-aggregate-error") < body.index("modal-f"), "the line sits in the body, above the footer"


def test_a_server_error_under_a_field_goes_when_that_field_is_edited(ctx, envelopes) -> None:
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "p-a")
    pick_two(ctx)
    reject_with(ctx, envelopes["conflict"])
    submit_and_settle(ctx)
    assert field_error(ctx, "Name") == TAKEN
    toggle(ctx, "p-c")
    assert field_error(ctx, "Name") == TAKEN, "picking a member does not answer a complaint about the name"
    type_(ctx, "mp-aggregate-name", "p-a2")
    assert field_error(ctx, "Name") is None, "the refusal was about the old name"


def test_a_members_refusal_goes_when_the_members_change(ctx, envelopes) -> None:
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "x")
    pick_two(ctx)
    reject_with(ctx, envelopes["member_not_found"])
    submit_and_settle(ctx)
    assert field_error(ctx, "Members") == GONE
    toggle(ctx, "p-b")
    assert field_error(ctx, "Members") == "Pick at least two profiles.", "the live rule speaks again"
    toggle(ctx, "p-c")
    assert field_error(ctx, "Members") is None


def test_the_press_is_busy_while_the_request_is_out_and_a_second_press_sends_nothing(ctx) -> None:
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "slow")
    pick_two(ctx)
    ctx.eval("ANSWER = function () { return new Promise(function (resolve) { PENDING.push(resolve); }); };")
    click(ctx, "mp-aggregate-submit")
    assert js(ctx, 'MR.find("mp-aggregate-submit").props.busy') is True
    assert js(ctx, 'MR.find("mp-aggregate-submit").props.disabled') is False, "the request is not folded into disabled"
    click(ctx, "mp-aggregate-submit")
    assert len(calls(ctx)) == 1
    ctx.eval("PENDING[0]({ id: 'slow' })")
    settle(ctx)
    assert saved_count(ctx) == 1 and closed_count(ctx) == 1


def test_a_save_that_lands_after_the_overlay_was_dismissed_reports_and_closes_nothing(ctx) -> None:
    """``onSaved`` and ``onClose`` are the host's (``NV_ProfileModalHost`` clears its one modal), so a save still out when the person dismisses the overlay must not call them when it lands: the
    host may be showing something else by then. The row WAS saved, so the toast still says so."""
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "late")
    pick_two(ctx)
    hold_the_request(ctx)
    click(ctx, "mp-aggregate-submit")
    dismiss(ctx, "mp-aggregate-cancel")
    assert closed_count(ctx) == 1, "Cancel's own call"
    ctx.eval("PENDING[0]({ id: 'late' })")
    settle(ctx)
    assert saved_count(ctx) == 0 and closed_count(ctx) == 1, "the host heard nothing from an overlay that was gone"
    assert toasts(ctx) == [{"kind": "success", "text": "Created aggregated profile late"}]


def test_a_press_while_the_form_is_not_ready_sends_nothing(ctx) -> None:
    mount(ctx)
    click(ctx, "mp-aggregate-submit")
    assert calls(ctx) == [] and toasts(ctx) == []


# ---- edit ----------------------------------------------------------------------------------------------------------------------------------------------


def test_an_aggregate_edit_keeps_every_field_the_form_does_not_show(ctx) -> None:
    mount(ctx, {"existing": FULL_AGG})
    assert title(ctx) == "Edit aggregated profile"
    assert prop(ctx, "mp-aggregate-name", "readOnly") is True and prop(ctx, "mp-aggregate-name", "value") == "agg-1"
    assert prop(ctx, "mp-aggregate-description", "value") == "my words"
    segment(ctx, "mp-aggregate-routing", "round_robin")
    click(ctx, "mp-aggregate-submit")
    sent = {k: v for k, v in FULL_AGG.items() if k not in ("agent_count", "graph_node_count", "_window")}
    assert calls(ctx) == [["PUT", "/model_profiles/agg-1", {**sent, "strategy": "round_robin"}]]
    assert toasts(ctx) == [{"kind": "success", "text": "Saved agg-1"}]
    assert saved_count(ctx) == 1 and closed_count(ctx) == 1


def test_an_edit_sends_the_members_in_the_order_shown_and_keeps_every_other_stored_field(ctx) -> None:
    """The edit's counterpart of the create test: the PUT carries the order on screen. p-c is ticked and moved to the front of [p-a, p-b], which makes [p-c, p-a, p-b]: not alphabetical
    either way, and not the stored order with p-c on the end. One failover choice changes as well, and every other field of the stored row (config, harness_id, the null provider
    fields, the description, the other failover choice) goes back as stored."""
    mount(ctx, {"existing": FULL_AGG})
    toggle(ctx, "p-c")
    click(ctx, "mp-member-up:p-c")
    click(ctx, "mp-member-up:p-c")
    shown = ["p-c", "p-a", "p-b"]
    assert_not_alphabetical(shown)
    assert ticked(ctx) == shown
    open_advanced(ctx)
    segment(ctx, "mp-aggregate-failover-on", "transient_and_config")
    click(ctx, "mp-aggregate-submit")
    stored = {k: v for k, v in FULL_AGG.items() if k not in ("agent_count", "graph_node_count", "_window")}
    assert calls(ctx) == [["PUT", "/model_profiles/agg-1", {**stored, "members": shown, "failover_on": "transient_and_config"}]]


def test_an_edit_sends_the_typed_description(ctx) -> None:
    mount(ctx, {"existing": FULL_AGG})
    type_(ctx, "mp-aggregate-description", "new words")
    click(ctx, "mp-aggregate-submit")
    assert calls(ctx)[0][2]["description"] == "new words"


def test_a_cleared_description_is_an_error_and_blocks_save(ctx) -> None:
    mount(ctx, {"existing": FULL_AGG})
    type_(ctx, "mp-aggregate-description", "  ")
    assert field_error(ctx, "Description") == "Enter a description."
    assert js(ctx, 'MR.find("mp-aggregate-submit").props.disabled') is True


def test_the_id_in_the_path_is_encoded(ctx) -> None:
    mount(ctx, {"existing": {**FULL_AGG, "id": "a b/c"}})
    click(ctx, "mp-aggregate-submit")
    assert calls(ctx)[0][1] == "/model_profiles/a%20b%2Fc"


def test_a_member_missing_from_the_list_survives_a_save(ctx) -> None:
    mount(ctx, {"existing": {**FULL_AGG, "members": ["p-a", "p-gone"]}})
    assert text_at(ctx, "mp-member-window:p-gone") == "unknown"
    assert text_at(ctx, "mp-aggregate-window") == "Unknown: a picked profile is not loaded."
    assert ticked(ctx) == ["p-a", "p-gone"]
    click(ctx, "mp-aggregate-submit")
    assert calls(ctx)[0][2]["members"] == ["p-a", "p-gone"]


def test_the_edited_aggregate_is_not_offered_as_its_own_member(ctx) -> None:
    mount(ctx, {"existing": FULL_AGG}, rows=[*ROWS, {"id": "agg-1", "kind": "single", "context_length": 99}])
    assert "agg-1" not in table_keys(ctx)


def test_a_conflict_on_an_edit_is_a_form_level_error(ctx, envelopes) -> None:
    mount(ctx, {"existing": FULL_AGG})
    reject_with(ctx, envelopes["conflict"])
    submit_and_settle(ctx)
    assert field_error(ctx, "Name") is None
    assert "p-a" in text_at(ctx, "mp-aggregate-error")
    assert closed_count(ctx) == 0


def test_an_edit_that_is_refused_under_members_names_the_field(ctx, envelopes) -> None:
    mount(ctx, {"existing": {**FULL_AGG, "members": ["p-a", "p-gone"]}})
    reject_with(ctx, envelopes["member_not_found"])
    submit_and_settle(ctx)
    assert field_error(ctx, "Members") == GONE


def test_a_harness_managed_aggregate_is_view_only(ctx) -> None:
    mount(ctx, {"existing": {**FULL_AGG, "harness_id": "h1"}})
    assert title(ctx) == "Aggregated profile"
    assert not present(ctx, "mp-aggregate-submit")
    assert text_at(ctx, "mp-aggregate-managed") == HARNESS_NOTE
    assert text_at(ctx, "mp-aggregate-cancel") == "Close"
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-members").props.rows.every(function (r) { return r.disabled === true; })') is True
    assert not present(ctx, "mp-member-up:p-a") and not present(ctx, "mp-member-down:p-b"), "no reorder buttons on a row that cannot change"
    assert present(ctx, "mp-member-order:p-a"), "the order is still told"
    assert prop(ctx, "mp-aggregate-description", "readOnly") is True


def test_a_managed_aggregate_is_wrapped_in_a_disabled_fieldset_and_shows_its_advanced_choices(ctx) -> None:
    fieldsets = 'allUnder("mp-aggregate").filter(function (n) { return n.type === "fieldset"; })'
    mount(ctx, {"existing": {**FULL_AGG, "harness_id": "h1"}})
    assert js(ctx, f"{fieldsets}.map(function (n) {{ return n.props.disabled; }})") == [True], "every native control under it, the kit's segmented buttons included, is read-only"
    assert js(ctx, 'MR.findProp("testid", "mp-aggregate-failover-point").props.value') == "mid_stream", "the folded choices are shown: a disabled fold cannot be opened"
    mount(ctx, {"existing": FULL_AGG})
    assert js(ctx, f"{fieldsets}.length") == 0, "an editable aggregate has no wrapper"


# ---- the list ------------------------------------------------------------------------------------------------------------------------------------------


def test_too_few_single_profiles_says_how_to_get_more(ctx) -> None:
    mount(ctx, rows=[{"id": "p-a", "kind": "single", "context_length": 1}, {"id": "agg-x", "kind": "aggregated"}])
    assert text_at(ctx, "mp-aggregate-too-few") == "Aggregating needs at least two LLM profiles. Register an LLM provider and import its models first."
    mount(ctx)
    assert not present(ctx, "mp-aggregate-too-few")


def test_loading_and_load_error_states(ctx) -> None:
    ctx.eval("LIST = { data: undefined, error: null, loading: true };")
    ctx.eval("mount({})")
    assert text_at(ctx, "mp-aggregate-loading") == "Loading the profiles"
    assert not present(ctx, "mp-aggregate-too-few") and not present(ctx, "mp-aggregate-load-error")
    envelope = {"type": "/errors/internal", "title": "Server error", "status": 500, "detail": "the list is down"}
    ctx.eval(f"LIST = {{ data: undefined, error: new window.primerApi.ApiError({json.dumps(envelope)}), loading: false }}; MR.rerender();")
    assert not present(ctx, "mp-aggregate-loading") and not present(ctx, "mp-aggregate-too-few")
    assert prop(ctx, "mp-aggregate-load-error", "role") == "alert"
    assert "the list is down" in text_at(ctx, "mp-aggregate-load-error")
    click(ctx, "mp-aggregate-load-retry")
    assert len(js(ctx, "RETRIES")) == 1


def test_try_again_says_it_is_trying_while_the_retry_is_out(ctx) -> None:
    """``useResource`` keeps ``error`` until the answer and turns ``loading`` on for the retry, so the alert is still up while the request is out and the button is the one place that can say so:
    busy (``aria-busy``, ``aria-disabled``, the press refused, still focusable) and "Trying again" until the answer comes."""
    failed = json.dumps({"type": "/errors/internal", "title": "Server error", "status": 500, "detail": "the list is down"})
    down = f"new window.primerApi.ApiError({failed})"
    retry = 'MR.find("mp-aggregate-load-retry").props'
    host = 'allUnder("mp-aggregate-load-retry").filter(function (n) { return n.type === "button"; })[0].props'

    ctx.eval(f"LIST = {{ data: undefined, error: {down}, loading: false }}; mount({{}});")
    assert text_at(ctx, "mp-aggregate-load-retry") == "Try again" and js(ctx, f"{retry}.busy") is False
    assert js(ctx, f'{host}["aria-busy"]') is None
    click(ctx, "mp-aggregate-load-retry")
    assert len(js(ctx, "RETRIES")) == 1

    ctx.eval(f"LIST = {{ data: undefined, error: {down}, loading: true }}; MR.rerender();")  # what the hook answers while the retry is out
    assert text_at(ctx, "mp-aggregate-load-retry") == "Trying again…"
    assert js(ctx, f"{retry}.busy") is True and js(ctx, f"{retry}.disabled") is None, "busy, never natively disabled"
    assert js(ctx, f'{host}["aria-busy"]') == "true" and js(ctx, f'{host}["aria-disabled"]') == "true"
    assert prop(ctx, "mp-aggregate-load-error", "role") == "alert" and "the list is down" in text_at(ctx, "mp-aggregate-load-error"), "the alert stays up through the retry"
    assert not present(ctx, "mp-aggregate-loading")
    press_host(ctx, "mp-aggregate-load-retry")
    assert len(js(ctx, "RETRIES")) == 1, "a press on a button that is busy asks for nothing more"

    ctx.eval(f"LIST = {{ data: undefined, error: {down}, loading: false }}; MR.rerender();")  # it failed again
    assert text_at(ctx, "mp-aggregate-load-retry") == "Try again" and js(ctx, f"{retry}.busy") is False
    press_host(ctx, "mp-aggregate-load-retry")
    assert len(js(ctx, "RETRIES")) == 2, "and when it is not busy a press asks again"


def test_a_list_that_failed_after_it_had_loaded_keeps_showing_the_loaded_rows(ctx) -> None:
    """``useResource`` keeps the data when a later poll fails (stale-while-error): the table stays, there is no error line."""
    mount(ctx)
    envelope = {"type": "/errors/internal", "title": "Server error", "status": 500, "detail": "later"}
    ctx.eval(f"LIST = {{ data: {{ items: {json.dumps(ROWS)} }}, error: new window.primerApi.ApiError({json.dumps(envelope)}), loading: false }}; MR.rerender();")
    assert not present(ctx, "mp-aggregate-load-error") and table_keys(ctx) == ["p-a", "p-b", "p-c"]


def test_the_overlay_asks_for_the_list_itself_once_and_does_not_poll(ctx) -> None:
    mount(ctx)
    asked = js(ctx, "ASKED.filter(function (a) { return a.key === 'mp-aggregate:profiles'; }).map(function (a) { return a.opts; })")
    assert asked and all(opts == {"pollMs": None} for opts in asked), "a dialog does not poll"
    ctx.eval("CALLS.length = 0; ASKED.filter(function (a) { return a.key === 'mp-aggregate:profiles'; })[0].fetcher('the-signal');")
    assert calls(ctx) == [["GET", "/model_profiles?limit=200", None]]


# ---- the dispatcher ------------------------------------------------------------------------------------------------------------------------------------


def test_the_dispatcher_routes_by_kind_and_open(ctx) -> None:
    mount(ctx, {"open": False})
    assert js(ctx, "MR.texts()") == [] and not present(ctx, "mp-aggregate"), "closed draws nothing"
    mount(ctx)
    assert present(ctx, "mp-aggregate"), "no profile: the create form is the aggregate's"
    mount(ctx, {"existing": FULL_AGG})
    assert present(ctx, "mp-aggregate")
    mount(ctx, {"existing": FULL_SINGLE, "providers": PROVIDERS})
    assert not present(ctx, "mp-aggregate") and present(ctx, "mp-single"), "a single profile is the single overlay's"
    # Plan 03 Task 6: the legacy single create (a `prefill`, the Providers page's old "New profile") is gone. A host that still passed one gets the one
    # create there is, the aggregate's: a single profile comes from the Register provider overlay's import.
    mount(ctx, {"prefill": {"provider_id": "lp"}, "providers": PROVIDERS})
    assert present(ctx, "mp-aggregate") and not present(ctx, "mp-single")


def test_a_different_profile_in_the_same_place_is_a_fresh_overlay(ctx) -> None:
    """The dispatcher keys the overlay on the profile's id: one mounted over another never inherits the first one's draft."""
    mount(ctx)
    type_(ctx, "mp-aggregate-name", "draft")
    ctx.eval(f"MR.rerender({{ open: true, existing: {json.dumps(FULL_AGG)}, providers: [], onSaved: function () {{}}, onClose: function () {{}} }})")
    assert prop(ctx, "mp-aggregate-name", "value") == "agg-1"
    assert title(ctx) == "Edit aggregated profile"


# ---- names ---------------------------------------------------------------------------------------------------------------------------------------------


def test_every_input_is_named_by_its_label(ctx) -> None:
    mount(ctx, {"existing": FULL_AGG})
    open_advanced(ctx)
    assert js(ctx, 'unlabelled("mp-aggregate")') == []
    mount(ctx)
    assert js(ctx, 'unlabelled("mp-aggregate")') == []
    assert js(ctx, 'allUnder("mp-aggregate").filter(function (n) { return n.type === "input"; }).length') >= 5, "the name, select-all and the three rows: the walk did reach them"
    mount(ctx, {"existing": FULL_SINGLE, "providers": PROVIDERS})
    assert js(ctx, 'unlabelled("mp-single")') == []
    kinds = js(ctx, 'allUnder("mp-single").filter(function (n) { return n.type === "input" || n.type === "select"; }).map(function (n) { return n.type; })')
    assert sorted(kinds) == ["input", "input", "input", "input", "select"], "id, description, model, context window and the provider select: the walk did reach them"


def test_every_button_that_is_only_an_arrow_has_a_name(ctx) -> None:
    mount(ctx)
    pick_two(ctx)
    arrows = js(ctx, 'allUnder("mp-aggregate").filter(function (n) { return n.type === "button" && /^[\\u2191\\u2193]$/.test(textOf(n)); }).map(function (n) { return n.props["aria-label"]; })')
    assert arrows == ["Move p-a up", "Move p-a down", "Move p-b up", "Move p-b down"]


# ---- the source ----------------------------------------------------------------------------------------------------------------------------------------


def test_the_file_exports_the_dispatcher_and_the_aggregate_overlay() -> None:
    for line in ("window.MP_ProfileModal = MP_ProfileModal;", "window.MP_AggregateOverlay = MP_AggregateOverlay;"):
        assert line in SOURCE


def test_the_overlays_use_kit_rows_and_btn_kind() -> None:
    single = SOURCE[SOURCE.index("function MP_SingleProfileOverlay("):SOURCE.index("function MP_AggregateOverlay(")]
    aggregate = SOURCE[SOURCE.index("function MP_AggregateOverlay("):SOURCE.index("\nwindow.MP_ProfileModal = ")]
    for body in (single, aggregate):
        assert "field-label" not in body, "every form row is a PK_Field"
        assert "variant=" not in body, "Btn takes kind, not variant"
        assert "useMutation" not in body, "the request is the overlay's own, with its own busy flag"


def test_the_file_holds_no_em_dash() -> None:
    assert "\u2014" not in SOURCE


# ---- the single profile's overlay (Task 3) --------------------------------------------------------------------------------------------------------------


def single(ctx, row, *, providers=PROVIDERS) -> None:
    mount(ctx, {"existing": row, "providers": providers})


def single_title(ctx) -> str:
    return prop(ctx, "mp-single", "aria-label")


def test_the_titles_and_buttons_of_the_single_overlay_follow_its_mode(ctx) -> None:
    single(ctx, FULL_SINGLE)
    assert single_title(ctx) == "Edit LLM profile" and prop(ctx, "mp-single", "className") == "modal pk-overlay"
    assert text_at(ctx, "mp-single-submit") == "Save" and text_at(ctx, "mp-single-cancel") == "Cancel"
    single(ctx, {**FULL_SINGLE, "harness_id": "h1"})
    assert single_title(ctx) == "LLM profile"


def test_the_fields_of_an_edit_hold_the_stored_values(ctx) -> None:
    single(ctx, FULL_SINGLE)
    assert prop(ctx, "mp-single-id", "value") == "p-a" and prop(ctx, "mp-single-id", "readOnly") is True
    assert prop(ctx, "mp-single-description", "value") == "old"
    assert prop(ctx, "mp-single-provider", "value") == "lp"
    assert prop(ctx, "mp-single-model", "value") == "qwen"
    assert prop(ctx, "mp-single-context", "value") == "32768" and prop(ctx, "mp-single-context", "type") == "number"
    assert js(ctx, 'MR.findProp("label", "Context window").props.help') == "The most tokens the model accepts in one request. Compaction uses it."
    assert text_at(ctx, "profile-modal-footnote") == FOOTNOTE


def test_the_provider_select_lists_the_llm_providers_by_id_and_type(ctx) -> None:
    single(ctx, FULL_SINGLE)
    options = js(ctx, 'allUnder("mp-single-provider").filter(function (n) { return n.type === "option"; }).map(function (n) { return [n.props.value, textOf(n)]; })')
    assert options == [["lp", "lp (openchat)"], ["other", "other (anthropic)"]]


def test_a_single_edit_keeps_every_field_the_form_does_not_show(ctx) -> None:
    single(ctx, FULL_SINGLE)
    type_(ctx, "mp-single-description", "new words")
    click(ctx, "mp-single-submit")
    sent = {k: v for k, v in FULL_SINGLE.items() if k not in ("agent_count", "graph_node_count")}
    assert calls(ctx) == [["PUT", "/model_profiles/p-a", {**sent, "description": "new words"}]], "config (reasoning, extended) goes back whole"
    assert toasts(ctx) == [{"kind": "success", "text": "Saved p-a"}]
    assert saved_count(ctx) == 1 and closed_count(ctx) == 1


def test_changing_the_model_and_the_window_changes_only_those(ctx) -> None:
    single(ctx, FULL_SINGLE)
    type_(ctx, "mp-single-model", "qwen-2")
    type_(ctx, "mp-single-context", "65536")
    ctx.eval('MR.find("mp-single-provider").props.onChange({ target: { value: "other" } }); MR.rerender();')
    click(ctx, "mp-single-submit")
    sent = {k: v for k, v in FULL_SINGLE.items() if k not in ("agent_count", "graph_node_count")}
    assert calls(ctx)[0][2] == {**sent, "model_name": "qwen-2", "context_length": 65536, "provider_id": "other"}


def test_a_managed_single_profile_offers_no_save(ctx) -> None:
    single(ctx, {**FULL_SINGLE, "harness_id": "h1"})
    assert not present(ctx, "mp-single-submit")
    assert text_at(ctx, "mp-single-managed") == HARNESS_NOTE
    assert text_at(ctx, "mp-single-cancel") == "Close"
    fieldsets = js(ctx, 'allUnder("mp-single").filter(function (n) { return n.type === "fieldset"; }).map(function (n) { return n.props.disabled; })')
    assert fieldsets == [True], "every control under it is read-only"
    single(ctx, FULL_SINGLE)
    assert js(ctx, 'allUnder("mp-single").filter(function (n) { return n.type === "fieldset"; }).length') == 0 and not present(ctx, "mp-single-managed")


def test_the_context_window_must_be_a_whole_number(ctx) -> None:
    single(ctx, FULL_SINGLE)
    type_(ctx, "mp-single-context", "0")
    assert field_error(ctx, "Context window") == WHOLE
    assert js(ctx, 'MR.find("mp-single-submit").props.disabled') is True
    type_(ctx, "mp-single-context", "")
    assert field_error(ctx, "Context window") == "Enter the context window."
    type_(ctx, "mp-single-context", "4096")
    assert field_error(ctx, "Context window") is None and js(ctx, 'MR.find("mp-single-submit").props.disabled') is False


def test_an_unlisted_provider_is_kept(ctx) -> None:
    single(ctx, {**FULL_SINGLE, "provider_id": "gone"})
    assert prop(ctx, "mp-single-provider", "value") == "gone"
    options = js(ctx, 'allUnder("mp-single-provider").filter(function (n) { return n.type === "option"; }).map(function (n) { return textOf(n); })')
    assert options == ["lp (openchat)", "other (anthropic)", "gone (not found)"]
    click(ctx, "mp-single-submit")
    assert calls(ctx)[0][2]["provider_id"] == "gone"


def test_a_refused_save_names_the_field(ctx, envelopes) -> None:
    single(ctx, FULL_SINGLE)
    reject_with(ctx, envelopes["provider_not_found"])
    click(ctx, "mp-single-submit")
    settle(ctx)
    assert "no-such-provider" in field_error(ctx, "Provider")
    assert field_error(ctx, "Description") is None and field_error(ctx, "Model") is None
    assert closed_count(ctx) == 0 and saved_count(ctx) == 0 and toasts(ctx) == []
    assert prop(ctx, "mp-single-description", "value") == "old"
    ctx.eval('MR.find("mp-single-provider").props.onChange({ target: { value: "other" } }); MR.rerender();')
    assert field_error(ctx, "Provider") is None, "the refusal was about the old choice"


def test_a_refusal_the_single_form_has_no_field_for_is_drawn_above_the_footer(ctx, envelopes) -> None:
    single(ctx, FULL_SINGLE)
    reject_with(ctx, envelopes["member_not_found"])
    click(ctx, "mp-single-submit")
    settle(ctx)
    assert prop(ctx, "mp-single-error", "role") == "alert" and "p-gone" in text_at(ctx, "mp-single-error")
    assert closed_count(ctx) == 0


def test_a_conflict_on_a_single_edit_is_a_form_level_error(ctx, envelopes) -> None:
    """Replaces test_a_taken_id_is_named_under_id_on_a_create (Plan 03 Task 6: the single overlay has no create, so it has no id to take). A 409 on an
    edit (a managed row, a path and body that disagree) is about the row, so it is a line above the footer, not an error under Id."""
    single(ctx, FULL_SINGLE)
    reject_with(ctx, envelopes["conflict"])
    click(ctx, "mp-single-submit")
    settle(ctx)
    assert field_error(ctx, "Id") is None
    assert text_at(ctx, "mp-single-error") == TAKEN and closed_count(ctx) == 0

def test_there_is_no_reasoning_control_until_task_7_and_config_is_kept_as_stored(ctx) -> None:
    """Model calls apply a profile's ``config`` now. The overlay does not draw a control for it yet: the Reasoning control returns in plan 03 Task 7, and until then ``config`` is kept as stored."""
    single(ctx, FULL_SINGLE)
    assert not present(ctx, "profile-reasoning-segment")
    assert "Reasoning" not in js(ctx, 'textOf(MR.find("mp-single"))'), "the Reasoning control returns in plan 03 Task 7; until then the form does not draw one"
    assert not present(ctx, "profile-model-picker") and not present(ctx, "profile-provider-picker"), "the discovered-models picker and the provider row picker are gone"
    click(ctx, "mp-single-submit")
    assert calls(ctx)[0][2]["config"] == FULL_SINGLE["config"], "a save sends config back as stored"


def test_a_blank_field_is_named_once_it_has_been_typed_in(ctx) -> None:
    single(ctx, FULL_SINGLE)
    for label in ("Id", "Description", "Model", "Context window"):
        assert field_error(ctx, label) is None, label
    type_(ctx, "mp-single-description", "")
    assert field_error(ctx, "Description") == "Enter a description."
    type_(ctx, "mp-single-model", " ")
    assert field_error(ctx, "Model") == "Enter the model name."


def test_the_single_press_is_busy_while_the_request_is_out_and_sends_once(ctx) -> None:
    single(ctx, FULL_SINGLE)
    ctx.eval("ANSWER = function () { return new Promise(function (resolve) { PENDING.push(resolve); }); };")
    click(ctx, "mp-single-submit")
    assert js(ctx, 'MR.find("mp-single-submit").props.busy') is True
    assert js(ctx, 'MR.find("mp-single-submit").props.disabled') is False, "the request is not folded into disabled"
    click(ctx, "mp-single-submit")
    assert len(calls(ctx)) == 1
    ctx.eval("PENDING[0]({ id: 'p-a' })")
    settle(ctx)
    assert saved_count(ctx) == 1 and closed_count(ctx) == 1


def test_a_single_save_that_lands_after_the_overlay_was_dismissed_reports_and_closes_nothing(ctx) -> None:
    single(ctx, FULL_SINGLE)
    hold_the_request(ctx)
    click(ctx, "mp-single-submit")
    dismiss(ctx, "mp-single-cancel")
    assert closed_count(ctx) == 1, "Cancel's own call"
    ctx.eval("PENDING[0]({ id: 'p-a' })")
    settle(ctx)
    assert saved_count(ctx) == 0 and closed_count(ctx) == 1, "the host heard nothing from an overlay that was gone"
    assert toasts(ctx) == [{"kind": "success", "text": "Saved p-a"}]


def test_the_single_cancel_closes_without_a_request(ctx) -> None:
    single(ctx, FULL_SINGLE)
    type_(ctx, "mp-single-description", "draft")
    click(ctx, "mp-single-cancel")
    assert closed_count(ctx) == 1 and calls(ctx) == []


def test_a_providers_list_that_changes_does_not_wipe_the_draft(ctx) -> None:
    """The old form re-seeded itself whenever the ``providers`` array it was handed was a new object; the host builds a new empty one on every render until the list loads."""
    single(ctx, FULL_SINGLE)
    type_(ctx, "mp-single-description", "mid-sentence")
    ctx.eval(f"MR.rerender({{ open: true, existing: {json.dumps(FULL_SINGLE)}, providers: {json.dumps(PROVIDERS)}, onSaved: function () {{}}, onClose: function () {{}} }})")
    assert prop(ctx, "mp-single-description", "value") == "mid-sentence"
