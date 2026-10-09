"""The mini React of ``tests/ui/_mini_react.py`` fails the way React does where a render test needs it to.

Two additions to the harness (review of #646): an object handed over as a CHILD throws ("Objects are not valid as a React child"), because that is how most of the crashes a pasted graph
spec can cause in the builder look, and a render test that silently skipped the object could not see them; and class components run (``setState``, ``componentDidMount``,
``componentDidUpdate``, ``static getDerivedStateFromError``), because an error boundary can only be a class.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._mini_react import mini_react_context

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
