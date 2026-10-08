"""What ``useFocusTrap`` DOES, driven in V8 against a small fake DOM (console review 2026-10-08, C-028, review of PR 515).

``tests/ui_e2e/test_overlay_focus_journey.py`` drives the trap in a real browser, but that lane does not run for a reviewer without
Playwright, and a journey that only checks "focus stayed inside" cannot tell a wrapping trap from one that never wraps. These tests run the
real ``ui/foundation/focus-trap.js`` against a fake page: a document order of elements, a ``document.activeElement``, a native Tab that
moves along that order unless the handler prevents it, and the two React hooks the trap uses (``useRef``, ``useEffect`` with cleanup).

What they pin, one behaviour each: Tab on the last element wraps to the first and Shift+Tab on the first wraps to the last (without the
wrap the native Tab leaves the dialog and the page behind it is reached); focus moves in on open; focus goes back to the element that had
it when the dialog opened, on close, and not to an element that is gone; an element that already holds focus inside (autoFocus) is kept,
while the opener is still the one restored; a Tab already handled by a nested dialog is left alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

HOOK = (Path(__file__).resolve().parents[2] / "ui" / "foundation" / "focus-trap.js").read_text(encoding="utf-8")

PRELUDE = r"""
var window = {};
var __h = { refs: [], effects: [], i: 0, pending: [] };
var React = {
  useRef: function (init) { var i = __h.i++; if (!(i in __h.refs)) __h.refs[i] = { current: init }; return __h.refs[i]; },
  useEffect: function (fn, deps) { __h.pending.push({ slot: __h.i++, fn: fn, deps: deps }); },
};

// ---- a fake page: elements in document order, native Tab along it ----
var PAGE = [];                       // every tabbable element, in document order
var document = {
  activeElement: null,
  contains: function (el) { return PAGE.indexOf(el) >= 0 || el.__node === true; },
};
function el(name, opts) {
  opts = opts || {};
  var e = { name: name, parent: opts.parent || null, visible: opts.visible !== false, listeners: {}, focusables: [] };
  e.focus = function () { document.activeElement = e; };
  e.contains = function (o) { while (o) { if (o === e) return true; o = o.parent; } return false; };
  e.querySelectorAll = function () { return e.focusables.slice(); };      // which descendants match the selector is the browser's job
  e.addEventListener = function (t, fn) { (e.listeners[t] = e.listeners[t] || []).push(fn); };
  e.removeEventListener = function (t, fn) { e.listeners[t] = (e.listeners[t] || []).filter(function (f) { return f !== fn; }); };
  Object.defineProperty(e, 'offsetParent', { get: function () { return e.visible ? {} : null; } });
  return e;
}
function press(node, shift) {
  // the browser dispatches keydown to the focused element; it reaches the dialog node by bubbling. Then the native default runs.
  var ev = { type: 'keydown', key: 'Tab', shiftKey: !!shift, defaultPrevented: false, preventDefault: function () { this.defaultPrevented = true; } };
  (node.listeners['keydown'] || []).forEach(function (fn) { fn(ev); });
  if (!ev.defaultPrevented) {
    var i = PAGE.indexOf(document.activeElement);
    var next = PAGE[(i + (shift ? -1 : 1) + PAGE.length) % PAGE.length];
    next.focus();
  }
  return ev;
}

// ---- a minimal commit loop for the hook ----
function render(ref, active, opts, deps) {
  __h.i = 0; __h.pending = [];
  window.primerApi.useFocusTrap(ref, active, opts, deps);
}
function commit() {
  __h.pending.forEach(function (p) {
    var prev = __h.effects[p.slot];
    var same = prev && prev.deps.length === p.deps.length && prev.deps.every(function (d, k) { return d === p.deps[k]; });
    if (same) return;
    if (prev && prev.cleanup) prev.cleanup();
    __h.effects[p.slot] = { deps: p.deps, cleanup: p.fn() };
  });
}
function unmount() {
  __h.effects.forEach(function (e) { if (e && e.cleanup) e.cleanup(); });
  __h.effects = []; __h.refs = [];
}

// ---- the scene: opener | dialog(first, middle, last) | after ----
function scene() {
  PAGE.length = 0;
  var opener = el('opener'), after = el('after');
  var dialog = el('dialog'); dialog.__node = true;
  var first = el('first', { parent: dialog }), middle = el('middle', { parent: dialog }), last = el('last', { parent: dialog });
  dialog.focusables = [first, middle, last];
  [opener, first, middle, last, after].forEach(function (x) { PAGE.push(x); });
  document.activeElement = opener;
  return { opener: opener, dialog: dialog, first: first, middle: middle, last: last, after: after };
}
function open(s, opts, deps) {
  var ref = { current: s.dialog };
  render(ref, true, opts || null, deps || []);
  return ref;
}
"""


@pytest.fixture()
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval(PRELUDE)
    c.eval(HOOK)
    yield c
    c.close()


def _ev(ctx, code: str):
    return ctx.eval(code)


def test_tab_on_the_last_element_wraps_to_the_first_and_does_not_leave_the_dialog(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      s.last.focus();
      var ev = press(s.dialog, false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "first"}, "Tab past the last element must come back to the first, not reach the page behind"


def test_shift_tab_on_the_first_element_wraps_to_the_last(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      s.first.focus();
      var ev = press(s.dialog, true);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "last"}


def test_shift_tab_with_focus_on_the_dialog_box_itself_goes_to_the_last_element(ctx) -> None:
    """The dialog node is focusable (tabindex -1): a click on its background puts focus there, and the native Shift+Tab from it would
    walk to whatever precedes the dialog in the page."""
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      s.dialog.focus();
      press(s.dialog, true);
      return document.activeElement.name;
    })()""")
    assert out == "last"


def test_tab_between_the_ends_is_left_to_the_browser(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      s.first.focus();
      var ev = press(s.dialog, false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": False, "now": "middle"}


def test_focus_moves_into_the_dialog_when_it_opens(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s); commit();
      return document.activeElement.name;
    })()""")
    assert out == "first"


def test_the_initial_option_names_the_element_that_gets_focus(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s, { initial: function (node) { return node.focusables[1]; } }); commit();
      return document.activeElement.name;
    })()""")
    assert out == "middle"


def test_focus_goes_back_to_the_opener_when_the_dialog_closes(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      var inside = document.activeElement.name;
      render(ref, false, null, []); commit();            // closed
      return { inside: inside, after: document.activeElement.name };
    })()""")
    assert out == {"inside": "first", "after": "opener"}, "focus must return to the element that had it when the dialog opened"


def test_focus_goes_back_to_the_opener_when_the_dialog_unmounts(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s); commit();
      unmount();
      return document.activeElement.name;
    })()""")
    assert out == "opener"


def test_an_opener_that_is_gone_is_not_focused(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      PAGE.splice(PAGE.indexOf(s.opener), 1);            // the opener left the document while the dialog was open
      s.middle.focus();
      render(ref, false, null, []); commit();
      return document.activeElement.name;
    })()""")
    assert out == "middle", "focus stays where it was; it is not sent to a detached element"


def test_an_autofocused_input_inside_keeps_focus_but_the_opener_is_still_restored(ctx) -> None:
    """React runs an autoFocus during commit, before any effect: by the time the trap's effect runs, focus is already inside the dialog,
    and the real opener can only have been captured during render."""
    out = _ev(ctx, """(function () {
      var s = scene();
      var ref = { current: s.dialog };
      render(ref, true, null, []);                       // render: the opener (document.activeElement) is captured here
      s.middle.focus();                                  // commit: autoFocus puts focus in the dialog
      commit();                                          // the effect runs
      var kept = document.activeElement.name;
      render(ref, false, null, []); commit();
      return { kept: kept, after: document.activeElement.name };
    })()""")
    assert out == {"kept": "middle", "after": "opener"}


def test_a_closed_dialog_does_not_trap_and_does_not_steal_focus(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = { current: s.dialog };
      render(ref, false, null, []); commit();
      s.last.focus();
      var ev = press(s.dialog, false);                   // no listener: the native default runs
      return { prevented: ev.defaultPrevented, now: document.activeElement.name, listeners: (s.dialog.listeners.keydown || []).length };
    })()""")
    assert out == {"prevented": False, "now": "after", "listeners": 0}


def test_a_tab_already_handled_by_a_nested_dialog_is_left_alone(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s); commit();
      s.last.focus();
      var ev = { type: 'keydown', key: 'Tab', shiftKey: false, defaultPrevented: true, preventDefault: function () {} };
      s.dialog.listeners.keydown.forEach(function (fn) { fn(ev); });
      return document.activeElement.name;
    })()""")
    assert out == "last", "a wrap another handler already did must not be wrapped a second time"


def test_other_keys_are_ignored(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s); commit();
      s.last.focus();
      var ev = { type: 'keydown', key: 'a', shiftKey: false, defaultPrevented: false, preventDefault: function () { this.defaultPrevented = true; } };
      s.dialog.listeners.keydown.forEach(function (fn) { fn(ev); });
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": False, "now": "last"}


def test_a_dialog_with_nothing_focusable_keeps_focus_on_the_box_and_swallows_tab(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); s.dialog.focusables = [];
      PAGE.length = 0; [s.opener, s.after].forEach(function (x) { PAGE.push(x); });
      open(s); commit();
      var landed = document.activeElement.name;
      var ev = press(s.dialog, false);
      return { landed: landed, prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"landed": "dialog", "prevented": True, "now": "dialog"}


def test_hidden_elements_are_not_tab_stops_so_the_wrap_goes_to_the_last_visible_one(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); s.last.visible = false;           // display:none: offsetParent is null
      PAGE.splice(PAGE.indexOf(s.last), 1);
      open(s); commit();
      s.middle.focus();
      var ev = press(s.dialog, false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "first"}


def test_the_hook_is_published_for_the_components_that_call_it(ctx) -> None:
    assert _ev(ctx, "typeof window.primerApi.useFocusTrap + '/' + typeof window.primerApi.focusablesOf") == "function/function"
