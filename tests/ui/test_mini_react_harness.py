"""The mini React of ``tests/ui/_mini_react.py`` fails the way React does where a render test needs it to.

Two additions to the harness (review of #646): an object handed over as a CHILD throws ("Objects are not valid as a React child"), because that is how most of the crashes a pasted graph
spec can cause in the builder look, and a render test that silently skipped the object could not see them; and class components run (``setState``, ``componentDidMount``,
``componentDidUpdate``, ``static getDerivedStateFromError``), because an error boundary can only be a class.

A third: the React APIs ``ui/components/shared/form-field.jsx`` calls, ``useId``, ``Children.toArray``, ``isValidElement`` and ``cloneElement``. Without them every test that mounted a component
drawing a ``FormField`` died on "FormField is not defined" and then on "Cannot read properties of undefined (reading 'toArray')", and the tests that did draw one carried a stub of each in
their own prelude. They are pinned here against what React 18.3.1 does (``ui/vendor/react.min.js``), and the real ``form-field.jsx`` is run on the bare harness at the end.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.ui._mini_react import ROOT, mini_react_context, transpile

_CODE = r"""
var h = React.createElement;
var log = [];
function Boom(props) { if (props.on) throw new Error("boom " + props.on); return h("span", { "data-testid": "ok" }, "fine"); }
class Boundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error: error }; }
  componentDidUpdate(prev) {
    log.push("didUpdate");
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }
  render() { return this.state.error ? h("p", { "data-testid": "fallback" }, "caught: " + this.state.error.message) : this.props.children; }
}
class Plain extends React.Component {
  render() { return this.props.children; }
}
class Counter extends React.Component {
  constructor(props) { super(props); this.state = { n: 0 }; }
  componentDidMount() { log.push("didMount"); this.setState({ n: 1 }); }
  render() { return h("b", { "data-testid": "n" }, "n=" + this.state.n); }
}
function App(props) { return h(Boundary, { resetKey: props.key2 }, h(Boom, { on: props.on })); }
function Bare(props) { return h(Plain, null, h(Boom, { on: props.on })); }
function ObjectChild() { return h("div", null, { not: "an element" }); }
function ObjectOut() { return { not: "an element" }; }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(_CODE)
    try:
        yield c
    finally:
        c.close()


def _texts(c) -> list[str]:
    return json.loads(c.eval("JSON.stringify(MR.texts())"))


def test_an_object_child_throws_as_react_does(ctx) -> None:
    with pytest.raises(Exception, match=r"Objects are not valid as a React child \(found: object with keys \{not\}\)"):
        ctx.eval("MR.mount(ObjectChild, {})")


def test_an_object_returned_from_a_component_throws(ctx) -> None:
    with pytest.raises(Exception, match="Objects are not valid as a React child"):
        ctx.eval("MR.mount(ObjectOut, {})")


def test_null_booleans_strings_numbers_and_arrays_of_them_are_still_fine(ctx) -> None:
    ctx.eval('function Fine() { return h("div", null, null, false, "a", 3, [ "b", null, 4 ]); } MR.mount(Fine, {})')
    assert _texts(ctx) == ["a", "3", "b", "4"]


def test_a_class_component_keeps_state_and_setstate_rerenders(ctx) -> None:
    ctx.eval("MR.mount(Counter, {})")
    assert _texts(ctx) == ["n=1"], "componentDidMount set the state once, and the tree rendered again"
    assert ctx.eval("log.join(',')") == "didMount"


def test_a_boundary_shows_its_fallback_for_a_throw_below_it_and_nothing_else_is_unmounted(ctx) -> None:
    ctx.eval('MR.mount(App, { on: "x", key2: 1 })')
    assert _texts(ctx) == ["caught: boom x"]


def test_a_boundary_with_no_error_renders_its_children(ctx) -> None:
    ctx.eval("MR.mount(App, { on: null, key2: 1 })")
    assert _texts(ctx) == ["fine"]


def test_a_boundary_tries_its_children_again_when_its_reset_key_changes(ctx) -> None:
    ctx.eval('MR.mount(App, { on: "x", key2: 1 })')
    assert _texts(ctx) == ["caught: boom x"]
    ctx.eval("MR.rerender({ on: null, key2: 1 })")
    assert _texts(ctx) == ["caught: boom x"], "the same reset key: the boundary stays on its fallback"
    ctx.eval("MR.rerender({ on: null, key2: 2 })")
    assert _texts(ctx) == ["fine"]
    ctx.eval('MR.rerender({ on: "y", key2: 3 })')
    assert _texts(ctx) == ["caught: boom y"], "and it catches the next throw"


def test_a_class_without_get_derived_state_from_error_lets_the_throw_through(ctx) -> None:
    with pytest.raises(Exception, match="boom z"):
        ctx.eval('MR.mount(Bare, { on: "z" })')


# ---------------------------------------------------------------------------
# useId, isValidElement, cloneElement, Children.toArray
# ---------------------------------------------------------------------------

_API_CODE = r"""
var h = React.createElement;
var seen = {};
function Field(props) {
  var id = React.useId();
  var count = React.useState(0);
  (seen[props.name] = seen[props.name] || []).push(id);
  return h("button", { "data-testid": "f-" + props.name, onClick: function () { count[1](count[0] + 1); } }, id);
}
function Two(props) { return h("div", null, h(Field, { name: "a" }), props.showB ? h(Field, { name: "b" }) : null); }
"""


@pytest.fixture
def api():
    c = mini_react_context(_API_CODE)
    try:
        yield c
    finally:
        c.close()


def _json(c, expr: str):
    return json.loads(c.eval(f"JSON.stringify({expr})"))


def _drawn(c, expr: str) -> list[str]:
    """The text a component that returns ``expr`` draws."""
    c.eval(f"function Out() {{ return {expr}; }} MR.mount(Out, {{}});")
    return _texts(c)


def test_use_id_is_one_id_per_component_instance_and_holds_across_renders(api) -> None:
    api.eval("MR.mount(Two, { showB: true });")
    api.eval('MR.rerender(); MR.click("f-a");')   # a re-render from above, then one from the component's own state
    seen = _json(api, "seen")
    assert len(seen["a"]) >= 3 and len(seen["b"]) >= 3, "the tree really drew again"
    assert len(set(seen["a"])) == 1 and len(set(seen["b"])) == 1, seen
    assert seen["a"][0] != seen["b"][0], "two instances of one component never share an id"
    assert re.fullmatch(r":r[0-9a-v]+:", seen["a"][0]), f"{seen['a'][0]!r} is not shaped like React's ids"


def test_a_component_that_leaves_the_tree_and_comes_back_gets_a_new_id(api) -> None:
    api.eval("MR.mount(Two, { showB: true });")
    api.eval("MR.rerender({ showB: false }); MR.rerender({ showB: true });")
    seen = _json(api, "seen")
    assert len(set(seen["a"])) == 1, "the component that stayed keeps its id"
    assert len(set(seen["b"])) == 2, "a remount is a new instance, with a new id"


def test_ids_start_over_in_each_context() -> None:
    first, second = mini_react_context(_API_CODE), mini_react_context(_API_CODE)
    try:
        first.eval("MR.mount(Two, { showB: true });")
        second.eval("MR.mount(Two, { showB: true });")
        assert _json(first, "seen") == _json(second, "seen"), "a test's ids do not depend on the tests that ran before it"
    finally:
        first.close()
        second.close()


def test_is_valid_element_is_true_for_elements_and_false_for_everything_else(api) -> None:
    got = _json(api, """[
      h("a"), h(Field, { name: "z" }), React.cloneElement(h("a"), {}), React.Children.toArray(h("a"))[0],
      null, undefined, "a", 1, true, {}, [], { __el: false }, function () {},
    ].map(function (x) { return React.isValidElement(x); })""")
    assert got == [True] * 4 + [False] * 9


def test_clone_element_lays_the_config_over_the_props_and_leaves_the_original_alone(api) -> None:
    got = _json(api, """(function () {
      var orig = h("input", { id: "a", title: "keep" });
      var copy = React.cloneElement(orig, { id: "b", "aria-invalid": "true" });
      return { copy: copy.props, type: copy.type, orig: orig.props, same: copy === orig, isElement: React.isValidElement(copy) };
    })()""")
    assert got == {"copy": {"id": "b", "title": "keep", "aria-invalid": "true"}, "type": "input", "orig": {"id": "a", "title": "keep"}, "same": False, "isElement": True}


def test_clone_element_keeps_the_key_and_the_ref_unless_the_config_gives_new_ones(api) -> None:
    got = _json(api, """(function () {
      var r1 = { current: 1 }, r2 = { current: 2 };
      var el = h("li", { key: "k1", ref: r1 });
      return {
        noConfig: React.cloneElement(el).key,
        undefinedKey: React.cloneElement(el, { key: undefined }).key,
        newKey: React.cloneElement(el, { key: "k2" }).key,
        undefinedRef: React.cloneElement(el, { ref: undefined }).props.ref === r1,
        newRef: React.cloneElement(el, { ref: r2 }).props.ref === r2,
        original: [el.key, el.props.ref === r1],
      };
    })()""")
    assert got == {"noConfig": "k1", "undefinedKey": "k1", "newKey": "k2", "undefinedRef": True, "newRef": True, "original": ["k1", True]}


def test_clone_element_children_replace_the_originals_and_are_what_draws(api) -> None:
    assert _drawn(api, 'React.cloneElement(h("p", null, "old", h("b", null, "bold")), { title: "t" })') == ["old", "bold"], "no new children: the element's own stay"
    assert _drawn(api, 'React.cloneElement(h("p", null, "old"), null, "new")') == ["new"]
    assert _drawn(api, 'React.cloneElement(h("p", null, "old"), null, "x", h("i", null, "y"))') == ["x", "y"]
    assert _drawn(api, 'React.cloneElement(h("p", null, "old"), { children: "from the config" })') == ["from the config"]
    assert _drawn(api, 'React.cloneElement(h("p", null, "old"), { children: "from the config" }, "from the call")') == ["from the call"], "the arguments after the config win"
    assert _json(api, 'React.cloneElement(h("p"), null, "a").props.children') == "a"
    assert _json(api, 'React.cloneElement(h("p"), null, "a", "b").props.children') == ["a", "b"]


@pytest.mark.parametrize("bad, shown", [("null", "null"), ("undefined", "undefined"), ('"text"', "text"), ("{ not: 1 }", "[object Object]")])
def test_clone_element_of_something_that_is_not_an_element_throws(api, bad: str, shown: str) -> None:
    with pytest.raises(Exception, match=re.escape(f"React.cloneElement(...): The argument must be a React element, but you passed {shown}.")):
        api.eval(f"React.cloneElement({bad}, {{}})")


def test_children_to_array_flattens_in_order_and_drops_what_react_drops(api) -> None:
    got = _json(api, """React.Children.toArray(
      ["a", [null, 2, [false, h("i")]], undefined, function () {}, h("b"), true]
    ).map(function (c) { return React.isValidElement(c) ? { type: c.type, key: c.key } : c; })""")
    assert got == ["a", 2, {"type": "i", "key": ".1:2:1"}, {"type": "b", "key": ".4"}]


def test_children_to_array_keys_an_element_by_its_position_or_by_its_own_key(api) -> None:
    got = _json(api, """(function () {
      var plain = h("a"), keyed = h("a", { key: "x" }), odd = h("a", { key: "y=z:w" });
      function keys(arr) { return arr.map(function (c) { return c.key; }); }
      return {
        list: keys(React.Children.toArray([plain, keyed, odd, plain])),
        lone: keys(React.Children.toArray(plain)),
        loneKeyed: keys(React.Children.toArray(keyed)),
        theElementsPassedIn: [plain.key, keyed.key],
      };
    })()""")
    assert got == {"list": [".0", ".$x", ".$y=0z=2w", ".3"], "lone": [".0"], "loneKeyed": [".$x"], "theElementsPassedIn": [None, "x"]}


def test_children_to_array_of_nothing_is_an_empty_list_and_a_string_or_number_is_itself(api) -> None:
    assert _json(api, '[null, undefined, false, "s", 0].map(function (c) { return React.Children.toArray(c); })') == [[], [], [], ["s"], [0]]


def test_children_to_array_throws_on_an_object_as_a_render_does(api) -> None:
    message = "Objects are not valid as a React child (found: object with keys {not})"
    with pytest.raises(Exception, match=re.escape(message)):
        api.eval('React.Children.toArray([h("a"), { not: "an element" }])')
    with pytest.raises(Exception, match=re.escape(message)):
        api.eval('MR.mount(function () { return h("div", null, { not: "an element" }); }, {})')


# ---------------------------------------------------------------------------
# the real FormField, on the bare harness
# ---------------------------------------------------------------------------

_FIELD_CODE = r"""
var h = React.createElement;
function Icon() { return null; }
function Host(props) {
  return h("div", { "data-testid": "host" }, h(FormField, props.field, props.object ? { not: "an element" } : h("textarea", { "data-testid": "ta" })));
}
function hostsOf(tag) {
  var out = [];
  (function w(n) {
    if (n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(w); return; }
    if (!n.__el) return;
    if (n.type === tag) {
      out.push({ id: n.props.id, htmlFor: n.props.htmlFor, role: n.props.role, className: n.props.className, describedBy: n.props["aria-describedby"], invalid: n.props["aria-invalid"] });
    }
    w(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  })(MR.find("host"));
  return out;
}
function view() { return JSON.stringify({ label: hostsOf("label"), textarea: hostsOf("textarea"), div: hostsOf("div") }); }
"""


@pytest.fixture(scope="module")
def form_field_code() -> str:
    return transpile(ROOT / "ui" / "components" / "shared" / "form-field.jsx")


@pytest.fixture
def field(form_field_code):
    c = mini_react_context(form_field_code, _FIELD_CODE)
    try:
        yield c
    finally:
        c.close()


def test_the_real_form_field_runs_on_the_bare_harness(field) -> None:
    """The row ``GR_ImportSpecModal`` and a dozen more forms draw: ``useId``, ``Children.toArray``, ``isValidElement`` and ``cloneElement`` working together, with no stub of any of them."""
    field.eval('MR.mount(Host, { field: { label: "Name", help: "A hint.", err: "Too short." } });')
    first = json.loads(field.eval("view()"))
    label, area = first["label"][0], first["textarea"][0]
    help_line = next(d for d in first["div"] if d.get("className") == "field-help" and d.get("role") is None)
    alert = next(d for d in first["div"] if d.get("role") == "alert")
    assert area["id"] and label["htmlFor"] == area["id"], "the label points at the control it names"
    assert area["invalid"] == "true", "the error is tied to the control"
    assert set(area["describedBy"].split()) == {help_line["id"], alert["id"]}, "so are the help line and the error"
    field.eval("MR.rerender();")
    assert json.loads(field.eval("view()")) == first, "every id holds across a re-render"


def test_the_real_form_field_still_throws_for_an_object_child(field) -> None:
    with pytest.raises(Exception, match="Objects are not valid as a React child"):
        field.eval('MR.mount(Host, { object: true, field: { label: "x" } });')
