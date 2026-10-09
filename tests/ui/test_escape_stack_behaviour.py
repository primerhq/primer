"""What ``useEscape`` DOES, driven in V8 against a fake window and a small commit loop (board task 01a12147).

Before the stack, the overlay panel and ``Modal`` each added their own window ``keydown`` listener, so ONE Escape closed every layer that was listening: a confirm dialog over the graph builder closed
the builder (an unsaved draft lost) and left the dialog on screen. ``ui/foundation/escape-stack.js`` has one listener that calls the handler of the TOP-MOST registered layer only. These tests run the
real file against a fake window (listeners, a dispatch that carries ``defaultPrevented``) and the two React hooks it uses (``useRef``, ``useEffect`` with a cleanup and a dependency list), with render
and commit as separate steps so that React's order (a parent renders first, a child's effects run first) can be reproduced.

What they pin, one behaviour each: the top layer alone hears Escape and the next one hears the next; the layer that became active last is on top however the effects ran; a handler that changes
between renders keeps its place and the latest one is called; an inactive layer is not on the stack and a layer that turns active goes on top; an Escape that was already handled, and any other key,
is not an Escape (so is one that belongs to an IME composition); a top layer that does nothing keeps everything below it; a layer closed and opened again goes back on top; a layer called
with one argument is active; a render that is never committed puts nothing on the stack; the window listener exists only while a layer does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

MODULE = (Path(__file__).resolve().parents[2] / "ui" / "foundation" / "escape-stack.js").read_text(encoding="utf-8")

PRELUDE = r"""
var __log = [];
var window = {
  _listeners: [],
  addEventListener: function (type, fn) { if (type === 'keydown') this._listeners.push(fn); },
  removeEventListener: function (type, fn) { if (type === 'keydown') this._listeners = this._listeners.filter(function (f) { return f !== fn; }); },
};
function press(key, prevented, extra) {
  var ev = { type: 'keydown', key: key, defaultPrevented: !!prevented, preventDefault: function () { this.defaultPrevented = true; } };
  Object.keys(extra || {}).forEach(function (k) { ev[k] = extra[k]; });
  window._listeners.slice().forEach(function (fn) { fn(ev); });
  return ev;
}

// ---- two hooks and a commit loop: render(inst) runs the component body and queues its effects; commit(inst) runs them like React ----
var __cur = null;
var React = {
  useRef: function (init) { var i = __cur.i++; if (!(i in __cur.refs)) __cur.refs[i] = { current: init }; return __cur.refs[i]; },
  useEffect: function (fn, deps) { __cur.queue.push({ slot: __cur.i++, fn: fn, deps: deps }); },
};
function layer(name, active, handler) {
  return { name: name, active: active, handler: handler, refs: [], i: 0, queue: [], deps: {}, cleanups: {}, mounted: false };
}
function render(inst) {
  __cur = inst; inst.i = 0; inst.queue = [];
  var handler = inst.handler === 'none' ? undefined : (inst.handler || function () { __log.push(inst.name); });
  if (inst.oneArg) window.primerApi.useEscape(handler); else window.primerApi.useEscape(handler, inst.active);
  __cur = null;
}
function commit(inst) {
  inst.queue.forEach(function (e) {
    var prev = inst.deps[e.slot];
    var same = prev && e.deps && prev.length === e.deps.length && prev.every(function (d, k) { return d === e.deps[k]; });
    if (same) return;
    if (inst.cleanups[e.slot]) inst.cleanups[e.slot]();
    inst.cleanups[e.slot] = e.fn();
    inst.deps[e.slot] = e.deps;
  });
  inst.queue = [];
  inst.mounted = true;
}
function mount(name, active, handler) { var inst = layer(name, active === undefined ? true : active, handler); render(inst); commit(inst); return inst; }
function mountWithOneArgument(name) { var inst = layer(name, true); inst.oneArg = true; render(inst); commit(inst); return inst; }
function rerender(inst, patch) { Object.keys(patch || {}).forEach(function (k) { inst[k] = patch[k]; }); render(inst); commit(inst); }
function unmount(inst) { Object.keys(inst.cleanups).forEach(function (k) { if (inst.cleanups[k]) inst.cleanups[k](); }); inst.cleanups = {}; }
function heard() { var l = __log.slice(); __log.length = 0; return l; }
"""


@pytest.fixture
def ctx():
    c = MiniRacer()
    c.eval(PRELUDE)
    c.eval(MODULE)
    try:
        yield c
    finally:
        c.close()


def _heard(ctx) -> list[str]:
    return json.loads(ctx.eval("JSON.stringify(heard())"))


def test_the_top_layer_alone_hears_escape_and_the_next_one_hears_the_next(ctx) -> None:
    """The reported defect: a confirm dialog over an overlay, one Escape. The dialog closes; the overlay is still there; the second Escape closes the overlay."""
    ctx.eval("var overlay = mount('overlay'); var dialog = mount('dialog');")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["dialog"]
    ctx.eval("unmount(dialog);")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["overlay"]
    ctx.eval("unmount(overlay);")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == []


def test_two_modals_on_top_of_each_other_close_top_first(ctx) -> None:
    ctx.eval("var a = mount('modal-a'); var b = mount('modal-b'); var c = mount('modal-c');")
    order = []
    for name in ("c", "b", "a"):
        ctx.eval("press('Escape');")
        order += _heard(ctx)
        ctx.eval(f"unmount({name});")
    assert order == ["modal-c", "modal-b", "modal-a"]


def test_a_layer_that_is_open_on_the_first_render_of_the_one_it_sits_in_is_still_on_top(ctx) -> None:
    """React runs a child's effects before its parent's, so by effect order the modal inside an overlay that opens with the modal already open would sit UNDER it. The order is the render order."""
    ctx.eval("var parent = layer('overlay', true); var child = layer('modal', true); render(parent); render(child); commit(child); commit(parent);")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["modal"]


def test_a_layer_that_turns_active_later_goes_on_top_of_one_opened_in_between(ctx) -> None:
    """The command palette is mounted from the start and opens over an overlay that was opened after it was first drawn: it must be on top when it opens."""
    ctx.eval("var palette = mount('palette', false); var overlay = mount('overlay');")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["overlay"], "a closed palette is not on the stack"
    ctx.eval("rerender(palette, { active: true });")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["palette"]
    ctx.eval("rerender(palette, { active: false });")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["overlay"]


def test_a_handler_that_changes_between_renders_keeps_its_place_and_the_latest_one_is_called(ctx) -> None:
    ctx.eval("var low = mount('low'); var high = mount('high');")
    ctx.eval("rerender(low, { handler: function () { __log.push('low, newer handler'); } });")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["high"], "re-rendering the lower layer does not lift it"
    ctx.eval("rerender(high, { handler: function () { __log.push('high, newer handler'); } });")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["high, newer handler"]
    ctx.eval("unmount(high); press('Escape');")
    assert _heard(ctx) == ["low, newer handler"]


def test_an_escape_that_was_already_handled_or_another_key_is_not_an_escape(ctx) -> None:
    """An input that closes its own suggestion list calls ``preventDefault`` on its Escape; the layers do not close as well."""
    ctx.eval("var only = mount('only');")
    ctx.eval("press('Escape', true); press('Enter'); press('Tab'); press('Esc');")
    assert _heard(ctx) == []
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["only"]


def test_a_top_layer_that_refuses_keeps_everything_below_it(ctx) -> None:
    """A dialog with a request in flight ignores Escape; the overlay under it must not hear the key instead."""
    ctx.eval("var overlay = mount('overlay'); var busy = mount('busy', true, function () {});")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == []


def test_a_layer_without_a_handler_is_the_top_and_closes_nothing_below_and_throws_nothing(ctx) -> None:
    """The bottom sheet's ``onClose`` is optional."""
    ctx.eval("var overlay = mount('overlay'); var sheet = mount('sheet', true, 'none');")
    assert ctx.eval("(function () { try { press('Escape'); return null; } catch (e) { return String(e); } })()") is None
    assert _heard(ctx) == []


def test_a_handler_that_closes_its_own_layer_does_not_pass_the_same_key_to_the_next(ctx) -> None:
    ctx.eval("var overlay = mount('overlay'); var dialog = mount('dialog', true, function () { __log.push('dialog'); unmount(dialog); });")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["dialog"]
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["overlay"]


def test_the_window_listener_exists_only_while_a_layer_does(ctx) -> None:
    assert ctx.eval("window._listeners.length") == 0
    ctx.eval("var a = mount('a'); var b = mount('b');")
    assert ctx.eval("window._listeners.length") == 1, "one listener for any number of layers"
    ctx.eval("unmount(b);")
    assert ctx.eval("window._listeners.length") == 1
    ctx.eval("unmount(a);")
    assert ctx.eval("window._listeners.length") == 0
    ctx.eval("var c = mount('c', false);")
    assert ctx.eval("window._listeners.length") == 0, "an inactive layer adds nothing"
    ctx.eval("rerender(c, { active: true });")
    assert ctx.eval("window._listeners.length") == 1


def test_the_hook_is_published_where_the_components_look_for_it(ctx) -> None:
    assert ctx.eval("typeof window.primerApi.useEscape") == "function"


@pytest.mark.parametrize("extra", [{"isComposing": True}, {"keyCode": 229}, {"isComposing": True, "keyCode": 229}], ids=["isComposing", "keyCode-229", "both"])
def test_an_escape_that_belongs_to_an_ime_composition_is_not_an_escape(ctx, extra) -> None:
    """Review of #700, B1: Escape cancels the composition in an input method editor (Chrome reports ``isComposing``, Safari ``keyCode`` 229 on the keydown that ends one); it must not close the layer too."""
    ctx.eval("var only = mount('only');")
    ctx.eval(f"press('Escape', false, {json.dumps(extra)});")
    assert _heard(ctx) == []
    ctx.eval("press('Escape', false, { isComposing: false, keyCode: 27 });")
    assert _heard(ctx) == ["only"], "the same key outside a composition closes it"


def test_a_layer_that_is_closed_and_opened_again_goes_back_on_top(ctx) -> None:
    """N1: A on, B on, A off, A on: the Escape is A's, then B's. (A mutant that keeps the first number of A would have B on top.)"""
    ctx.eval("var a = mount('a'); var b = mount('b'); rerender(a, { active: false }); rerender(a, { active: true });")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["a"]
    ctx.eval("unmount(a); press('Escape');")
    assert _heard(ctx) == ["b"]


def test_a_layer_called_with_one_argument_is_active(ctx) -> None:
    """N2: ``Modal``, ``NV_OverlayPanel`` and ``NV_Lightbox`` call ``useEscape(handler)``: ``active`` defaults to true."""
    ctx.eval("var overlay = mountWithOneArgument('overlay'); var dialog = mountWithOneArgument('dialog');")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["dialog"]
    ctx.eval("unmount(dialog); press('Escape');")
    assert _heard(ctx) == ["overlay"]


def test_a_render_that_is_never_committed_puts_nothing_on_the_stack(ctx) -> None:
    """N4: the order is taken at render, and a render React throws away must neither add a layer nor lift one; only a commit puts a layer on the stack."""
    ctx.eval("var low = mount('low'); var abandoned = layer('abandoned', true); render(abandoned);")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["low"]
    assert ctx.eval("window._listeners.length") == 1
    ctx.eval("commit(abandoned);")
    ctx.eval("press('Escape');")
    assert _heard(ctx) == ["abandoned"], "committed later, so on top"
